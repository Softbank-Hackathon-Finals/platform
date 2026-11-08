"""Read-only deployment tracing: namespace → workload → service account → workload identity
→ policy → resources (design 8.11, ADR-0034 slot ``source.deploy_trace``).

This module is the kind-neutral core: the output shape, the Kubernetes export walk and the
env-name matching. What a service account's identity reaches is kind-specific and lives in
``bravo_inventory.sources``:

* ``aws``    IRSA role ARN → IAM policy → S3 · Secrets Manager · KMS (``FileAdapter`` · ``Boto3Adapter``)
* ``gcp``    Workload Identity GSA → IAM bindings → GCS · Secret Manager · Cloud KMS
* ``onprem`` OpenBao Kubernetes auth role → policy paths, SeaweedFS ``s3.json`` identity → buckets

``sources.deploy_adapter_for(kind, ...)`` picks the adapter; an unregistered kind is
``UNSUPPORTED``. Every adapter records the files it read and the APIs it called so the inventory
can prove "no write API was used" (T16 할 일).
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .static_scan import safe_endpoint

ENV_SOURCE_KEYS = ("secretKeyRef", "configMapKeyRef", "fieldRef", "resourceFieldRef")

# Resource kinds a workload setting can name (bucket, secret, key), per source kind. A granted
# resource of these kinds that no setting names gets its own deploy-only item (reconcile).
ENV_NAMED_KINDS: frozenset[str] = frozenset(
    {"s3_bucket", "secret", "kms_key",  # aws
     "gcs_bucket", "gcp_secret", "gcp_kms_key",  # gcp
     "seaweedfs_bucket", "openbao_secret", "openbao_transit_key"}  # onprem
)
# Resource kinds that are public-cloud managed services (``cloud_dependency`` on deploy refs).
CLOUD_RESOURCE_KINDS: frozenset[str] = frozenset(
    {"s3_bucket", "secret", "kms_key", "iam_role", "gcs_bucket", "gcp_secret", "gcp_kms_key",
     "gcp_service_account", "gcp_project_role"}
)


@dataclass
class Resource:
    kind: str  # s3_bucket | secret | kms_key | iam_role | gcs_bucket | gcp_secret | ... | cnpg_cluster | image
    name: str
    role: str
    via: str  # how we got here, e.g. "ServiceAccount/chat → IAMRole/chat-irsa → policy chat-s3"
    actions: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class DeployGraph:
    namespace: str
    workloads: list[dict[str, Any]] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    api_calls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (workload, env name, literal value) kept in memory only to match env settings against
    # resource names. Never serialised: ``as_dict`` does not include it.
    env_literals: list[tuple[str, str, str]] = field(default_factory=list, repr=False)
    # Kind of the adapter that traced the graph (ADR-0034). Kept in memory for reconcile and
    # written by ``bravo-inventory trace``. Not part of ``as_dict``: that is the inventory's
    # ``deploy`` and the input of ``deploy_digest``, so an unchanged graph keeps its digest (the
    # document already records the kind as ``source.env_kind``).
    source_kind: str = "aws"

    def as_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "workloads": self.workloads,
            "resources": [asdict(r) for r in self.resources],
            "api_calls": sorted(set(self.api_calls)),
            "notes": self.notes,
        }


class DeployAdapter(Protocol):
    def trace(self, namespace: str) -> DeployGraph: ...


def _items(doc: Any) -> list[dict]:
    if isinstance(doc, dict) and doc.get("kind", "").endswith("List"):
        return list(doc.get("items", []))
    if isinstance(doc, dict):
        return [doc]
    return list(doc or [])


def merge_resources(resources: list[Resource]) -> list[Resource]:
    """One entry per (kind, name, via): a bucket ARN and its bucket/* object ARN name the same
    bucket. Actions are unioned; the first entry's attributes are kept."""
    out: dict[tuple[str, str, str], Resource] = {}
    for r in resources:
        key = (r.kind, r.name, r.via)
        if key in out:
            out[key].actions = sorted(set(out[key].actions) | set(r.actions))
        else:
            r.actions = sorted(set(r.actions))
            out[key] = r
    return list(out.values())


_merge_resources = merge_resources  # T16 name

SECRET_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{6}$")  # Secrets Manager appends 6 random chars to the ARN


def _names_resource(value: str, r: Resource) -> bool:
    value = value.strip()
    if not value:
        return False
    names = {r.name, r.attributes.get("arn", ""), r.attributes.get("resource", "")} - {""}
    if r.kind == "secret":
        names.add(SECRET_SUFFIX_RE.sub("", r.name))
    if value in names:
        return True
    return "*" in r.name and fnmatch.fnmatchcase(value, r.name)


def mark_env_references(g: DeployGraph) -> None:
    """Record which workload setting names a resource (``Deployment/x:S3_BUCKET``). Only the
    env *name* is written; the compared value already is the resource name (or is not copied)."""
    for r in g.resources:
        if r.kind not in ENV_NAMED_KINDS:
            continue
        refs = sorted({f"Deployment/{w}:{name}" for w, name, value in g.env_literals if _names_resource(value, r)})
        if refs:
            r.attributes["referenced_by_env"] = ", ".join(refs)


def _env_summary(containers: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Names and *sources* of environment variables, never their values. A literal value that
    is a URL/DSN is reduced to ``scheme://host[:port]`` under ``endpoints`` (user info dropped)."""
    sources: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for c in containers:
        for e in c.get("env", []):
            name = e.get("name")
            if not name:
                continue
            value_from = e.get("valueFrom") or {}
            source = next((k for k in ENV_SOURCE_KEYS if k in value_from), None)
            if source is None:
                source = "literal" if "value" in e else "unknown"
            sources[name] = source
            if source == "literal" and "://" in str(e.get("value", "")):
                endpoints[name] = safe_endpoint(str(e["value"]))
    return sources, endpoints


class ExportReader:
    """Reads JSON exports from one directory and remembers which files existed and were read."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.read: list[str] = []

    def load(self, name: str) -> Any:
        p = self.dir / name
        if not p.is_file():
            return None
        self.read.append(f"file:{name}")
        return json.loads(p.read_text(encoding="utf-8"))

    def text(self, name: str) -> str | None:
        p = self.dir / name
        if not p.is_file():
            return None
        self.read.append(f"file:{name}")
        return p.read_text(encoding="utf-8")


# follow(graph, service account name, service account object, via) adds what the identity reaches.
Follow = Callable[[DeployGraph, str, dict, str], None]


def walk_namespace(
    reader: ExportReader,
    namespace: str,
    source_kind: str,
    follow: Follow,
    has_identity: Callable[[str, dict], bool],
    unused_note: str = "Deployment 미사용: SecretStore · 운영 작업 등",
) -> DeployGraph:
    """Deployments → service accounts → ``follow``; service accounts no Deployment uses but that
    carry an identity (``has_identity``) are followed too; CNPG clusters. Kind-neutral."""
    g = DeployGraph(namespace, source_kind=source_kind)
    deployments = [d for d in _items(reader.load("deployments.json")) if d.get("metadata", {}).get("namespace", namespace) == namespace]
    sas = {sa["metadata"]["name"]: sa for sa in _items(reader.load("serviceaccounts.json")) if sa.get("metadata", {}).get("namespace", namespace) == namespace}
    used: set[str] = set()
    for d in deployments:
        spec = d.get("spec", {}).get("template", {}).get("spec", {})
        sa_name = spec.get("serviceAccountName", "default")
        used.add(sa_name)
        containers = spec.get("containers", [])
        env_names = sorted({e["name"] for c in containers for e in c.get("env", []) if "name" in e})
        env_sources, endpoints = _env_summary(containers)
        g.env_literals.extend((d["metadata"]["name"], e["name"], str(e["value"])) for c in containers for e in c.get("env", []) if "name" in e and "value" in e)
        images = [c.get("image", "") for c in containers]
        g.workloads.append({"name": d["metadata"]["name"], "service_account": sa_name, "images": images, "env": env_names, "env_sources": env_sources, "endpoints": endpoints})
        for image in images:
            g.resources.append(Resource("image", image, "registry", f"Deployment/{d['metadata']['name']}", [], {"digest_pinned": "yes" if "@sha256:" in image else "no"}))
        follow(g, sa_name, sas.get(sa_name, {}), f"Deployment/{d['metadata']['name']} → ServiceAccount/{sa_name}")
    # An identity in the namespace that no Deployment uses still reaches resources for it (ESO
    # SecretStore, operator jobs). Its grants are deploy-side dependencies too.
    for sa_name in sorted(set(sas) - used):
        if has_identity(sa_name, sas[sa_name]):
            follow(g, sa_name, sas[sa_name], f"ServiceAccount/{sa_name} ({unused_note})")
    for cl in _items(reader.load("clusters.json")):
        if cl.get("metadata", {}).get("namespace", namespace) == namespace:
            g.resources.append(Resource("cnpg_cluster", cl["metadata"]["name"], "db", f"Namespace/{namespace}", [], {"instances": str(cl.get("spec", {}).get("instances", "")), "wal_level": str(cl.get("spec", {}).get("postgresql", {}).get("parameters", {}).get("wal_level", ""))}))
    return g


def annotations(sa: dict) -> dict[str, str]:
    return sa.get("metadata", {}).get("annotations", {}) or {}


def apply_attributes(resources: list[Resource], data: dict, sections: dict[str, str], allowed: dict[str, frozenset[str]]) -> None:
    """Copy allowlisted readback attributes (``data[section][name]``) onto resources."""
    for r in resources:
        section = sections.get(r.kind)
        if section and r.name in data.get(section, {}):
            r.attributes.update({k: str(v) for k, v in data[section][r.name].items() if k in allowed[r.kind]})


# T16 names: the AWS adapter moved to ``sources.aws`` (ADR-0034). Resolved lazily so this core
# module does not import a kind-specific one at import time.
_AWS_NAMES = frozenset({"FileAdapter", "Boto3Adapter", "READ_ONLY_API", "IRSA_ANNOTATION", "ARN_RESOURCE_RE", "RESOURCE_ATTRIBUTES"})


def __getattr__(name: str) -> Any:
    if name in _AWS_NAMES:
        from .sources import aws

        return getattr(aws, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
