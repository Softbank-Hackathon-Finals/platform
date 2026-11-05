"""Assemble, digest and validate the inventory document."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from .capabilities import fit, load_profile, observe, resolve
from .deploy_trace import DeployAdapter, DeployGraph
from .reconcile import reconcile
from .static_scan import scan_repo

SCHEMA = json.loads(resources.files("bravo_inventory").joinpath("schemas/inventory.schema.json").read_text(encoding="utf-8"))


def _digest(data: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def repo_digest(root: Path, app_path: str) -> str:
    h = hashlib.sha256()
    base = (root / app_path).resolve()
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not p.is_symlink() and not any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in p.relative_to(root).parts):
            h.update(rel.encode())
            h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


def validate_inventory(doc: dict) -> None:
    jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker()).validate(doc)


def build_inventory(
    *,
    app_id: str,
    repo: Path,
    app_path: str,
    source_env: dict[str, str],
    adapter: DeployAdapter | None,
    profiles: list[Path],
    exports: dict[str, Path] | None = None,
    source_sha: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Pure function of its inputs: same repo, exports and profiles → same document (except time)."""
    repo = Path(repo).resolve()
    findings = scan_repo(repo, app_path)
    graph: DeployGraph | None = adapter.trace(source_env["namespace"]) if adapter else None
    loaded = [load_profile(p) for p in profiles]
    candidates = []
    items_by_kind: dict[str, list[dict]] = {}
    for prof in loaded:
        kind = prof["env_kind"]
        if kind not in items_by_kind:
            items_by_kind[kind] = [i.as_dict() for i in reconcile(findings, graph, kind)]
        caps = resolve(prof, observe((exports or {}).get(prof["env_id"])))
        f = fit(items_by_kind[kind], prof, caps)
        f["capabilities"] = caps
        candidates.append(f)
    # Primary item list uses the first candidate's kind (P0: onprem) so the console has one list.
    primary_kind = loaded[0]["env_kind"] if loaded else "onprem"
    items = items_by_kind.get(primary_kind) or [i.as_dict() for i in reconcile(findings, graph, primary_kind)]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
    deploy = graph.as_dict() if graph else {"namespace": source_env["namespace"], "workloads": [], "resources": [], "api_calls": [], "notes": ["배포 추적 없음: 어댑터 미지정"]}
    doc = {
        "schema_version": "1.0",
        "app": {"id": app_id, "repo": str(repo), "app_path": app_path, "source_sha": source_sha},
        "source": dict(source_env),
        "generated_at": (now or datetime.now(UTC)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "inputs": {"repo_digest": repo_digest(repo, app_path), "deploy_digest": _digest(deploy), "api_calls": deploy["api_calls"]},
        "items": items,
        "deploy": deploy,
        "candidates": candidates,
        "summary": {
            "grade_counts": counts,
            "agent_needed": sum(1 for i in items if i["agent_needed"]),
            "deploy_only": sum(1 for i in items if i["found_in"] == "deploy"),
            "code_only": sum(1 for i in items if i["found_in"] == "code"),
        },
    }
    doc["digest"] = _digest({k: v for k, v in doc.items() if k != "generated_at"})
    validate_inventory(doc)
    return doc
