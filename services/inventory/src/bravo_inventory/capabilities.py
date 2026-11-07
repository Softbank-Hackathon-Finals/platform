"""Target-environment capability detection (8.11 등록: 제공 기능 감지).

A profile file declares what the operator registered; optional Kubernetes exports let the
platform *observe* some of it (operator CRDs, known workloads). Observed values win and the
source of each value is recorded so the console can show "native", "declared" or "observed".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import CATALOG, GRADES, target_entry

KNOWN_WORKLOADS = {
    "seaweedfs": ("s3_compatible", "seaweedfs"),
    "minio": ("s3_compatible", "minio"),
    "openbao": ("secret_store", "openbao"),
    "vault": ("secret_store", "vault"),
    "external-secrets": ("secrets_injection", "eso"),
    "kyverno": ("policy", "kyverno"),
    "otel-collector": ("observability", "otel"),
    "opentelemetry-collector": ("observability", "otel"),
}
KNOWN_CRDS = {"clusters.postgresql.cnpg.io": ("postgres_operator", "cnpg"), "externalsecrets.external-secrets.io": ("secrets_injection", "eso"), "clusterpolicies.kyverno.io": ("policy", "kyverno")}

# Per target kind: which capability backs a role's replacement (checked by ``fit``), and which
# capabilities the kind offers natively as a managed service (no registration needed). Cluster
# add-ons of the common base (CNPG, ESO, Kyverno) are never native: they are declared or observed.
_REQUIRES = {"object_store": "s3_compatible", "secrets": "secret_store", "db": "postgres_operator"}
KIND_CAPABILITIES: dict[str, dict[str, dict[str, str]]] = {
    "onprem": {"requires": dict(_REQUIRES), "native": {}},
    "aws": {
        "requires": dict(_REQUIRES),
        "native": {"s3_compatible": "s3", "secret_store": "secrets-manager", "kms": "kms", "audit": "cloudtrail"},
    },
    "gcp": {
        "requires": dict(_REQUIRES),
        "native": {"s3_compatible": "gcs-s3-interop", "secret_store": "secret-manager", "kms": "cloud-kms", "audit": "cloud-audit-logs"},
    },
}


def load_profile(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("env_id", "env_kind"):
        if key not in data:
            raise ValueError(f"profile missing {key}")
    if data["env_kind"] not in CATALOG or data["env_kind"] not in KIND_CAPABILITIES:
        raise ValueError(f"unknown env_kind {data['env_kind']}")
    data.setdefault("provides", {})
    return data


def observe(exports_dir: Path | None) -> dict[str, dict[str, str]]:
    """Return {capability: {value, source}} observed from exports (deployments.json,
    statefulsets.json, crds.json). Stateful services such as OpenBao usually run as StatefulSets."""
    found: dict[str, dict[str, str]] = {}
    if not exports_dir:
        return found
    for file, kind in (("deployments.json", "Deployment"), ("statefulsets.json", "StatefulSet")):
        dep = exports_dir / file
        if not dep.is_file():
            continue
        doc = json.loads(dep.read_text(encoding="utf-8"))
        items = doc.get("items", [doc]) if isinstance(doc, dict) else doc
        for d in items:
            name = d.get("metadata", {}).get("name", "").lower()
            for key, (cap, value) in KNOWN_WORKLOADS.items():
                if key in name and cap not in found:
                    found[cap] = {"value": value, "source": f"observed: {kind}/{name}"}
    crds = exports_dir / "crds.json"
    if crds.is_file():
        doc = json.loads(crds.read_text(encoding="utf-8"))
        items = doc.get("items", [doc]) if isinstance(doc, dict) else doc
        for c in items:
            name = c.get("metadata", {}).get("name", "")
            if name in KNOWN_CRDS:
                cap, value = KNOWN_CRDS[name]
                found[cap] = {"value": value, "source": f"observed: CRD/{name}"}
    return found


def resolve(profile: dict[str, Any], observed: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """native (managed service of the kind) < declared (profile) < observed (exports)."""
    kind = profile.get("env_kind", "")
    out: dict[str, dict[str, str]] = {k: {"value": v, "source": f"native: {kind}"} for k, v in KIND_CAPABILITIES.get(kind, {}).get("native", {}).items()}
    out.update({k: {"value": str(v), "source": "declared: profile"} for k, v in profile.get("provides", {}).items()})
    out.update(observed)
    return out


def fit(items: list[dict], profile: dict[str, Any], caps: dict[str, dict[str, str]], source_kind: str | None = None) -> dict[str, Any]:
    """Rule-only fitness for one candidate target (T32 input). AI never changes 'blocked'.

    With ``source_kind`` the result also names the pair and whether it is a same-kind move
    (another cluster of the same kind), so candidates of any kind are compared the same way.
    """
    env_kind = profile["env_kind"]
    requires = KIND_CAPABILITIES.get(env_kind, {}).get("requires", {})
    counts = {g: 0 for g in GRADES}
    missing: list[str] = []
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
        entry = (target_entry(source_kind, env_kind, it["role"]) if source_kind else target_entry(env_kind, it["role"])) or {}
        need = entry.get("impl", "")
        if need and it["role"] in requires:
            cap_key = requires[it["role"]]
            msg = f"{it['role']} 대체({need})를 뒷받침할 '{cap_key}' 기능이 등록되지 않음"
            if cap_key not in caps and msg not in missing:  # one line per role, however many items it has
                missing.append(msg)
    verdict = "blocked" if counts["none"] or missing else ("needs_work" if counts["code_change"] or counts["unknown"] else "fits")
    out = {"env_id": profile["env_id"], "env_kind": env_kind, "verdict": verdict, "grade_counts": counts, "missing_capabilities": missing, "residual_dependencies": profile.get("residual_dependencies", [])}
    if source_kind:
        out["source_kind"] = source_kind
        out["migration"] = "same_kind_cluster" if source_kind == env_kind else "cross_kind"
    return out
