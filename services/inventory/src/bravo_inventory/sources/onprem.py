"""On-prem source deploy trace (ADR-0034 ``source.deploy_trace`` for ``onprem``):

* ServiceAccount → OpenBao Kubernetes auth role (``bound_service_account_names`` /
  ``bound_service_account_namespaces``) → token policies → secret paths and transit keys.
* ServiceAccount → SeaweedFS ``s3.json`` identity → bucket actions.

``OnpremFileAdapter`` reads exports only (no OpenBao token or SeaweedFS admin key is handed to
this module). Files, any may be absent::

    deployments.json · serviceaccounts.json · clusters.json
    openbao.json  {"auth_mount": "kubernetes",
                   "kubernetes_roles": {"<role>": {"bound_service_account_names": [...],
                                                   "bound_service_account_namespaces": [...],
                                                   "token_policies": [...]}},
                   "policies": {"<name>": "<HCL text>" | {"path": {"<path>": {"capabilities": [...]}}}}}
    s3.json       SeaweedFS identities file. Only ``name`` and ``actions`` are read; credentials
                  are never copied.
    s3-bindings.json  {"<service account>": "<identity name>"} when names differ.

A SeaweedFS identity is bound to a service account by ``s3-bindings.json``, the service account
annotation ``bravo.dev/seaweedfs-identity``, or the same name. Identities no workload is bound to
are listed in ``notes`` by name, never guessed.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..deploy_trace import (
    DeployGraph,
    ExportReader,
    Resource,
    annotations,
    mark_env_references,
    merge_resources,
    walk_namespace,
)

SEAWEEDFS_IDENTITY_ANNOTATION = "bravo.dev/seaweedfs-identity"
HCL_PATH_RE = re.compile(r'path\s+"([^"]+)"\s*\{[^}]*?capabilities\s*=\s*\[([^\]]*)\]', re.S)
TOKEN_SELF_PATHS = frozenset({"auth/token/lookup-self", "auth/token/renew-self", "auth/token/revoke-self"})
TRANSIT_RE = re.compile(r"^([^/]+)/(?:encrypt|decrypt|rewrap|datakey/[^/]+|keys|sign|verify|hmac)/(.+)$")
KV2_RE = re.compile(r"^([^/]+)/(?:data|metadata)/(.+)$")
SEAWEEDFS_ACTIONS = frozenset({"Read", "Write", "List", "Tagging", "Admin"})


def policy_paths(policy: Any) -> dict[str, list[str]]:
    """``{path: capabilities}`` from an OpenBao policy given as HCL text or as parsed JSON."""
    if isinstance(policy, str):
        return {m.group(1): re.findall(r'"([^"]+)"', m.group(2)) for m in HCL_PATH_RE.finditer(policy)}
    if isinstance(policy, dict):
        paths = policy.get("path", {})
        return {p: list((v or {}).get("capabilities", [])) for p, v in paths.items()} if isinstance(paths, dict) else {}
    return {}


def path_resource(path: str, capabilities: list[str], via: str) -> Resource | None:
    if path in TOKEN_SELF_PATHS:
        return None  # every token manages itself; not a dependency
    if path in {"*", "+"} or path.startswith(("*", "sys/", "auth/")):
        return Resource("wildcard", path, "iam", via, capabilities, {"warning": "policy reaches management or every path"})
    m = TRANSIT_RE.match(path)
    if m and m.group(1).startswith("transit"):
        return Resource("openbao_transit_key", m.group(2), "kms", via, capabilities, {"path": path})
    m = KV2_RE.match(path)
    name = f"{m.group(1)}/{m.group(2)}" if m else path
    return Resource("openbao_secret", name, "secrets", via, capabilities, {"path": path})


def _matches(patterns: Any, value: str) -> bool:
    values = [patterns] if isinstance(patterns, str) else list(patterns or [])
    return any(fnmatch.fnmatchcase(value, p) for p in values)


def bound_roles(openbao: dict, sa_name: str, namespace: str) -> list[tuple[str, dict]]:
    roles = openbao.get("kubernetes_roles", {}) or {}
    return [(name, spec) for name, spec in sorted(roles.items())
            if _matches(spec.get("bound_service_account_names"), sa_name)
            and _matches(spec.get("bound_service_account_namespaces"), namespace)]


def seaweedfs_bucket_actions(identity: dict) -> tuple[dict[str, list[str]], list[str]]:
    """``({bucket: [actions]}, [actions on every bucket])`` of one ``s3.json`` identity."""
    per_bucket: dict[str, set[str]] = {}
    everywhere: set[str] = set()
    for action in identity.get("actions", []) or []:
        verb, _, target = str(action).partition(":")
        if verb not in SEAWEEDFS_ACTIONS:
            continue
        if not target or target == "*":
            everywhere.add(verb)
        else:
            per_bucket.setdefault(target.split("/", 1)[0], set()).add(verb)
    return {b: sorted(v) for b, v in per_bucket.items()}, sorted(everywhere)


class OnpremFileAdapter:
    kind = "onprem"

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def trace(self, namespace: str) -> DeployGraph:
        reader = ExportReader(self.dir)
        openbao = reader.load("openbao.json") or {}
        s3 = reader.load("s3.json") or {}
        bindings = reader.load("s3-bindings.json") or {}
        identities = {i.get("name"): i for i in s3.get("identities", []) if isinstance(i, dict) and i.get("name")}
        mount = openbao.get("auth_mount", "kubernetes")
        policies = openbao.get("policies", {}) or {}
        anonymous_read = set(seaweedfs_bucket_actions(identities.get("anonymous", {}))[0])
        bound_identities: set[str] = set()

        def identity_for(sa_name: str, sa: dict) -> str | None:
            name = bindings.get(sa_name) or annotations(sa).get(SEAWEEDFS_IDENTITY_ANNOTATION) or sa_name
            return name if name in identities and name != "anonymous" else None

        def follow(g: DeployGraph, sa_name: str, sa: dict, via: str) -> None:
            roles = bound_roles(openbao, sa_name, namespace)
            identity = identity_for(sa_name, sa)
            if not roles and not identity:
                g.notes.append(f"{via}: OpenBao Kubernetes 역할 · SeaweedFS 신원 연결 없음")
            for role_name, spec in roles:
                token_policies = list(spec.get("token_policies", []) or spec.get("policies", []) or [])
                role_via = f"{via} → OpenBaoRole/{mount}/{role_name}"
                g.resources.append(Resource("openbao_role", f"{mount}/{role_name}", "iam", via, [], {"policies": ", ".join(token_policies)}))
                for pol in token_policies:
                    if pol not in policies:
                        g.notes.append(f"{role_via}: 정책 {pol} 내보내기 없음")
                        continue
                    for path, caps in sorted(policy_paths(policies[pol]).items()):
                        r = path_resource(path, caps, f"{role_via} → policy {pol}")
                        if r:
                            g.resources.append(r)
            if identity:
                bound_identities.add(identity)
                id_via = f"{via} → SeaweedFSIdentity/{identity}"
                g.resources.append(Resource("seaweedfs_identity", identity, "iam", via, [], {}))
                per_bucket, everywhere = seaweedfs_bucket_actions(identities[identity])
                if everywhere:
                    g.resources.append(Resource("wildcard", "*", "iam", id_via, everywhere, {"warning": "identity acts on every bucket"}))
                for bucket, actions in sorted(per_bucket.items()):
                    g.resources.append(Resource("seaweedfs_bucket", bucket, "object_store", id_via, actions, {"public": str(bucket in anonymous_read)}))

        def has_identity(sa_name: str, sa: dict) -> bool:
            return bool(bound_roles(openbao, sa_name, namespace)) or identity_for(sa_name, sa) is not None

        g = walk_namespace(reader, namespace, self.kind, follow, has_identity)
        unbound = sorted(set(identities) - bound_identities - {"anonymous"})
        if unbound:
            g.notes.append(f"SeaweedFS 신원 {', '.join(unbound)}: 워크로드 연결 미확인(s3-bindings.json 또는 SA 주석 {SEAWEEDFS_IDENTITY_ANNOTATION} 필요)")
        g.resources = merge_resources(g.resources)
        mark_env_references(g)
        g.api_calls = sorted(set(reader.read))
        return g
