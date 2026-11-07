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
from .catalog import CATALOG, LEGACY_DEFAULT_TARGET
from .deploy_trace import DeployAdapter, DeployGraph
from .grading import _env_names, apply_pair
from .reconcile import reconcile
from .static_scan import scan_repo, scanned_files

SCHEMA = json.loads(resources.files("bravo_inventory").joinpath("schemas/inventory.schema.json").read_text(encoding="utf-8"))


def _digest(data: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def repo_digest(root: Path, app_path: str) -> str:
    """Hash of exactly the files the scanners read (Python under ``app_path`` and configuration
    repo-wide), keyed by repo-relative path so the value does not depend on the checkout location."""
    root = Path(root).resolve()
    h = hashlib.sha256()
    for p in scanned_files(root, app_path):
        rel = p.relative_to(root).as_posix().encode()
        data = p.read_bytes()
        h.update(len(rel).to_bytes(8, "big") + rel + len(data).to_bytes(8, "big") + data)
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
    repo_ref: str | None = None,
    source_sha: str | None = None,
    now: datetime | None = None,
    target_kind: str | None = None,
) -> dict:
    """Pure function of its inputs: same repo, exports and profiles → same document (except time).

    Items are graded for the pair (``source_env["env_kind"]``, target kind) through the catalog
    hub. The primary item list uses the first candidate's kind, else ``target_kind``.
    """
    repo = Path(repo).resolve()
    findings = scan_repo(repo, app_path)
    graph: DeployGraph | None = adapter.trace(source_env["namespace"]) if adapter else None
    source_kind = source_env["env_kind"]
    env_names = _env_names(findings)
    loaded = [load_profile(p) for p in profiles]

    def graded(kind: str) -> list[dict]:
        return [i.as_dict() for i in apply_pair(reconcile(findings, graph, kind), source_kind, kind, env_names)]

    candidates = []
    items_by_kind: dict[str, list[dict]] = {}
    for prof in loaded:
        kind = prof["env_kind"]
        if kind not in items_by_kind:
            items_by_kind[kind] = graded(kind)
        caps = resolve(prof, observe((exports or {}).get(prof["env_id"])))
        f = fit(items_by_kind[kind], prof, caps, source_kind)
        f["capabilities"] = caps
        candidates.append(f)
    # Primary item list: first candidate's kind, else the named target kind. Only a caller that
    # names neither gets the legacy default (kept so older callers keep their output).
    primary_kind = loaded[0]["env_kind"] if loaded else (target_kind or LEGACY_DEFAULT_TARGET)
    if primary_kind not in CATALOG:
        raise ValueError(f"unknown target kind {primary_kind}")
    items = items_by_kind.get(primary_kind) or graded(primary_kind)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
    deploy = graph.as_dict() if graph else {"namespace": source_env["namespace"], "workloads": [], "resources": [], "api_calls": [], "notes": ["배포 추적 없음: 어댑터 미지정"]}
    doc = {
        "schema_version": "1.0",
        # Registered repo reference, never the local checkout path (home directory, checkout-dependent digest).
        "app": {"id": app_id, "repo": repo_ref or repo.name, "app_path": app_path, "source_sha": source_sha},
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
