"""Detect what a registered Kubernetes environment provides, with read-only GETs only.

설계 8.11 등록: 제공 기능 감지(K8s 버전 · Postgres 오퍼레이터 · S3 호환 저장소 · 시크릿 저장소 ·
CIS 프로필). The caller hands in ``get(path, params)`` that performs one HTTP GET against the
Kubernetes API and returns the parsed JSON (or raises). This module never builds a request body,
never chooses a verb and refuses any path outside ``READ_PATHS`` before calling ``get``.

Output uses the inventory ``capabilities`` shape: ``{capability: {"value", "source"}}``.

- ``observed: ...``  the API showed it (value is what was seen).
- ``observed: ...`` with value ``absent``  the API answered completely and the thing is not there
  (only where an answer is authoritative: served API groups, RKE2 node arguments).
- ``unknown: ...`` with value ``unknown``  denied, missing, truncated or inconclusive. No guessing.

Discovery (``/version``, ``/apis``) is readable by every authenticated subject by default. Nodes,
cluster-wide Services and SecretStores need an extra read grant; without it they stay ``unknown``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

UNKNOWN = "unknown"
ABSENT = "absent"
DETECTED = ("k8s_version", "postgres_operator", "s3_compatible", "secret_store", "cis_profile")

KubeGet = Callable[[str, "dict[str, str] | None"], Any]

# Every path this module may read. Templates use {group}/{version} from the served discovery list.
READ_PATHS: tuple[str, ...] = (
    "/version",
    "/apis",
    "/api/v1/nodes",
    "/api/v1/services",
    "/apis/external-secrets.io/{version}/clustersecretstores",
    "/apis/external-secrets.io/{version}/secretstores",
)
_READ_RE = [re.compile("^" + re.escape(p).replace(r"\{version\}", r"v[0-9a-z]+") + "$") for p in READ_PATHS]

POSTGRES_GROUPS = {"postgresql.cnpg.io": "cnpg", "acid.zalan.do": "zalando", "postgres-operator.crunchydata.com": "crunchy"}
S3_GROUPS = {"seaweed.seaweedfs.com": "seaweedfs", "minio.min.io": "minio", "objectbucket.io": "objectbucket"}
SECRET_STORE_GROUPS = {"secrets.hashicorp.com": "vault"}
INJECTION_GROUPS = {"external-secrets.io": "eso"}
S3_SERVICES = ("seaweedfs", "minio")
SECRET_SERVICES = ("openbao", "vault")
NODE_ARGS = "rke2.io/node-args"
LIST_LIMIT = "500"


class ReadRefused(PermissionError):
    """A path outside READ_PATHS was requested. Raised before any call is made."""


def _cap(value: str, source: str) -> dict[str, str]:
    return {"value": value, "source": source}


def _unknown(reason: str) -> dict[str, str]:
    return _cap(UNKNOWN, f"unknown: {reason}")


class _Reader:
    def __init__(self, get: KubeGet):
        self._get = get
        self.calls: list[str] = []

    def read(self, path: str, params: dict[str, str] | None = None) -> Any:
        if not any(r.match(path) for r in _READ_RE):
            raise ReadRefused(f"path outside the read-only list: {path}")
        self.calls.append(path)
        return self._get(path, params)

    def try_read(self, path: str, params: dict[str, str] | None = None) -> Any | None:
        try:
            doc = self.read(path, params)
        except ReadRefused:
            raise
        except Exception:  # denied, not found, transport: the caller records "unknown", never the error text
            return None
        return doc if isinstance(doc, dict) else None


def _items(doc: dict | None) -> list[dict] | None:
    """Items of a complete list response, or None when absent, malformed or truncated."""
    if not doc or (doc.get("metadata") or {}).get("continue"):
        return None
    items = doc.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return None
    return items


def _served_groups(apis: dict | None) -> dict[str, str] | None:
    """{group name: preferred version} from ``/apis``; None if the answer is unusable."""
    if not apis or apis.get("kind") != "APIGroupList" or not isinstance(apis.get("groups"), list):
        return None
    out: dict[str, str] = {}
    for g in apis["groups"]:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str):
            return None
        pref = (g.get("preferredVersion") or {}).get("version") or ""
        out[g["name"]] = pref if isinstance(pref, str) else ""
    return out


def _from_groups(groups: dict[str, str] | None, table: dict[str, str]) -> str | None:
    if groups is None:
        return None
    for name, value in table.items():
        if name in groups:
            return value
    return None


def _version(r: _Reader) -> dict[str, str]:
    doc = r.try_read("/version")
    v = (doc or {}).get("gitVersion")
    if isinstance(v, str) and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*", v):
        return _cap(v, "observed: /version")
    return _unknown("/version not readable")


def _postgres(groups: dict[str, str] | None) -> dict[str, str]:
    if groups is None:
        return _unknown("/apis not readable")
    found = _from_groups(groups, POSTGRES_GROUPS)
    if found:
        group = next(g for g, v in POSTGRES_GROUPS.items() if v == found)
        return _cap(found, f"observed: /apis group {group}")
    # The served-group list is complete for the cluster, so "not served" is an observation.
    return _cap(ABSENT, "observed: /apis (no known Postgres operator group served)")


def _service_names(r: _Reader) -> list[str] | None:
    items = _items(r.try_read("/api/v1/services", {"limit": LIST_LIMIT}))
    if items is None:
        return None
    return [str((i.get("metadata") or {}).get("namespace", "")) + "/" + str((i.get("metadata") or {}).get("name", "")).lower() for i in items]


def _match_service(names: list[str] | None, keys: tuple[str, ...]) -> tuple[str, str] | None:
    for key in keys:
        for name in sorted(names or []):
            if key in name.split("/", 1)[1]:
                return key, name
    return None


def _s3(groups: dict[str, str] | None, services: list[str] | None) -> dict[str, str]:
    found = _from_groups(groups, S3_GROUPS)
    if found:
        group = next(g for g, v in S3_GROUPS.items() if v == found)
        return _cap(found, f"observed: /apis group {group}")
    hit = _match_service(services, S3_SERVICES)
    if hit:
        return _cap(hit[0], f"observed: Service/{hit[1]}")
    # An S3-compatible store may live outside the cluster: no in-cluster trace is not "absent".
    return _unknown("no S3-compatible API group or Service observed" if services is not None else "services not readable")


def _secret_store(r: _Reader, groups: dict[str, str] | None, services: list[str] | None) -> dict[str, str]:
    hit = _match_service(services, SECRET_SERVICES)
    if hit:
        return _cap(hit[0], f"observed: Service/{hit[1]}")
    found = _from_groups(groups, SECRET_STORE_GROUPS)
    if found:
        return _cap(found, "observed: /apis group secrets.hashicorp.com")
    version = (groups or {}).get("external-secrets.io")
    if version:
        for kind in ("clustersecretstores", "secretstores"):
            items = _items(r.try_read(f"/apis/external-secrets.io/{version}/{kind}", {"limit": LIST_LIMIT}))
            for item in sorted(items or [], key=lambda i: str((i.get("metadata") or {}).get("name", ""))):
                provider = ((item.get("spec") or {}).get("provider") or {})
                if isinstance(provider, dict) and "vault" in provider:  # OpenBao is served through the Vault provider
                    return _cap("vault-api", f"observed: {kind[:-1]}/{(item.get('metadata') or {}).get('name', '')} provider vault")
    return _unknown("no secret store Service, API group or vault SecretStore observed")


def _node_profile(node: dict) -> str | None:
    """'' when the RKE2 node args carry no --profile, the profile value, or None if not readable."""
    raw = ((node.get("metadata") or {}).get("annotations") or {}).get(NODE_ARGS)
    try:
        args = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return None
    for i, a in enumerate(args):
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
        if a == "--profile" and i + 1 < len(args):
            return args[i + 1]
    return ""


def _cis(r: _Reader) -> dict[str, str]:
    nodes = _items(r.try_read("/api/v1/nodes", {"limit": LIST_LIMIT}))
    if not nodes:
        return _unknown("nodes not readable")
    profiles = {_node_profile(n) for n in nodes}
    if None in profiles:
        return _unknown(f"{NODE_ARGS} missing on some node (not RKE2 or not exposed)")
    if len(profiles) != 1:
        return _unknown("nodes disagree on the RKE2 profile")
    profile = profiles.pop()
    if not profile:
        return _cap(ABSENT, f"observed: Node {NODE_ARGS} (no --profile)")
    return _cap(f"rke2-{profile}", f"observed: Node {NODE_ARGS} --profile")


def detect_capabilities(get: KubeGet) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Return ({capability: {value, source}}, paths read). Always reports the five DETECTED keys;
    ``secrets_injection`` is added only when ESO's API group is served."""
    r = _Reader(get)
    groups = _served_groups(r.try_read("/apis"))
    services = _service_names(r)
    caps = {
        "k8s_version": _version(r),
        "postgres_operator": _postgres(groups),
        "s3_compatible": _s3(groups, services),
        "secret_store": _secret_store(r, groups, services),
        "cis_profile": _cis(r),
    }
    eso = _from_groups(groups, INJECTION_GROUPS)
    if eso:
        caps["secrets_injection"] = _cap(eso, "observed: /apis group external-secrets.io")
    return caps, r.calls


def provided(cap: dict[str, str] | None) -> bool:
    """A capability counts for fit only when it has a real value."""
    return bool(cap) and cap.get("value") not in (UNKNOWN, ABSENT, "", None)
