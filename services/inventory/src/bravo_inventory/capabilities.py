"""Target-environment capability detection (8.11 등록: 제공 기능 감지).

A profile file declares what the operator registered; optional Kubernetes exports let the
platform *observe* some of it (operator CRDs, known workloads, and the read-only API answers that
``k8s_capabilities.detect_capabilities`` reads). Observed values win, except that an ``unknown``
observation never replaces a declared value. The source of each value is recorded so the console
can show "declared" vs "observed" vs "unknown".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .catalog import CATALOG, GRADES
from .k8s_capabilities import ABSENT, UNKNOWN, detect_capabilities, provided

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
# Raw API answers saved next to the exports (``kubectl get --raw <path> > <file>``).
API_EXPORTS = {
    "/version": "version.json",
    "/apis": "apis.json",
    "/api/v1/nodes": "nodes.json",
    "/api/v1/services": "services.json",
    "clustersecretstores": "clustersecretstores.json",
    "secretstores": "secretstores.json",
}
KNOWN_CRDS = {"clusters.postgresql.cnpg.io": ("postgres_operator", "cnpg"), "externalsecrets.external-secrets.io": ("secrets_injection", "eso"), "clusterpolicies.kyverno.io": ("policy", "kyverno")}


def load_profile(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("env_id", "env_kind"):
        if key not in data:
            raise ValueError(f"profile missing {key}")
    if data["env_kind"] not in CATALOG:
        raise ValueError(f"unknown env_kind {data['env_kind']}")
    data.setdefault("provides", {})
    return data


def _file_get(exports_dir: Path):
    def get(path: str, params: dict[str, str] | None = None) -> object:
        name = API_EXPORTS.get(path) or API_EXPORTS[path.rsplit("/", 1)[-1]]
        return json.loads((exports_dir / name).read_text(encoding="utf-8"))  # missing file → unknown

    return get


def observe(exports_dir: Path | None) -> dict[str, dict[str, str]]:
    """Return {capability: {value, source}} observed from exports (deployments.json,
    statefulsets.json, crds.json). Stateful services such as OpenBao usually run as StatefulSets.
    When raw API answers (version.json · apis.json · nodes.json …) are present, the read-only
    detector also runs and reports the five registration capabilities, ``unknown`` included."""
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
    if any((exports_dir / API_EXPORTS[p]).is_file() for p in ("/version", "/apis", "/api/v1/nodes")):
        detected, _ = detect_capabilities(_file_get(exports_dir))
        for cap, val in detected.items():
            if provided(val) or not provided(found.get(cap)):  # a positive export is not undone by unknown
                found[cap] = val
    return found


def resolve(profile: dict[str, Any], observed: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {k: {"value": str(v), "source": "declared: profile"} for k, v in profile.get("provides", {}).items()}
    for cap, val in observed.items():
        if val.get("value") == UNKNOWN and cap in out:
            continue  # could not observe: keep what the operator registered
        out[cap] = val
    return out


def fit(items: list[dict], profile: dict[str, Any], caps: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Rule-only fitness for one candidate target (T32 input). AI never changes 'blocked'."""
    env_kind = profile["env_kind"]
    counts = {g: 0 for g in GRADES}
    missing: list[str] = []
    for it in items:
        counts[it["grade"]] = counts.get(it["grade"], 0) + 1
        need = CATALOG.get(env_kind, {}).get(it["role"], {}).get("impl", "")
        if need and it["role"] in {"object_store", "secrets", "db"}:
            cap_key = {"object_store": "s3_compatible", "secrets": "secret_store", "db": "postgres_operator"}[it["role"]]
            state = "관측되지 않음" if (caps.get(cap_key) or {}).get("value") == ABSENT else "등록되지 않음"
            msg = f"{it['role']} 대체({need})를 뒷받침할 '{cap_key}' 기능이 {state}"
            if not provided(caps.get(cap_key)) and msg not in missing:  # one line per role, however many items it has
                missing.append(msg)
    verdict = "blocked" if counts["none"] or missing else ("needs_work" if counts["code_change"] or counts["unknown"] else "fits")
    return {"env_id": profile["env_id"], "env_kind": env_kind, "verdict": verdict, "grade_counts": counts, "missing_capabilities": missing, "residual_dependencies": profile.get("residual_dependencies", [])}
