"""Read-only deployment tracing: namespace → workload → service account → cloud identity
→ policy → resources (design 8.11). Two adapters share one output shape:

* ``FileAdapter``  reads ``kubectl get -o json`` exports and an ``aws.json`` fixture.
* ``Boto3Adapter`` (optional extra ``aws``) calls a fixed allowlist of read-only APIs and
  records every call so the inventory can prove "no write API was used" (T16 할 일).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .catalog import role_for_service
from .static_scan import safe_endpoint

# Attributes copied from the cloud readback into the inventory. Anything else observed about a
# resource stays out of the document (the inventory is shared with the console and the model).
RESOURCE_ATTRIBUTES: dict[str, frozenset[str]] = {
    "s3_bucket": frozenset({"public", "region", "versioning", "object_lock"}),
    "secret": frozenset({"rotation_enabled", "kms_key_id"}),
    "kms_key": frozenset({"key_manager", "key_state", "key_spec"}),
}
ENV_SOURCE_KEYS = ("secretKeyRef", "configMapKeyRef", "fieldRef", "resourceFieldRef")

READ_ONLY_API: frozenset[str] = frozenset(
    {
        "iam:GetRole",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketLocation",
        "secretsmanager:DescribeSecret",
        "kms:DescribeKey",
    }
)

IRSA_ANNOTATION = "eks.amazonaws.com/role-arn"
ARN_RESOURCE_RE = re.compile(r"^arn:aws:(s3|secretsmanager|kms):[^:]*:[^:]*:(.*)$")


@dataclass
class Resource:
    kind: str  # s3_bucket | secret | kms_key | iam_role | cnpg_cluster | image
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


def _policy_resources(statements: list[dict], via: str) -> list[Resource]:
    out: list[Resource] = []
    for st in statements:
        if st.get("Effect", "Allow") != "Allow":
            continue
        actions = st.get("Action", [])
        actions = [actions] if isinstance(actions, str) else list(actions)
        resources = st.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else list(resources)
        for arn in resources:
            m = ARN_RESOURCE_RE.match(arn)
            if arn == "*":
                out.append(Resource("wildcard", "*", "iam", via, actions, {"warning": "wildcard resource"}))
                continue
            if not m:
                continue
            service, tail = m.group(1), m.group(2)
            kind = {"s3": "s3_bucket", "secretsmanager": "secret", "kms": "kms_key"}[service]
            name = tail.split("/", 1)[0] if service == "s3" else tail.removeprefix("secret:").removeprefix("key/")
            out.append(Resource(kind, name, role_for_service(service), via, actions, {"arn": arn}))
    return out


def _merge_resources(resources: list[Resource]) -> list[Resource]:
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


SECRET_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{6}$")  # Secrets Manager appends 6 random chars to the ARN


def _names_resource(value: str, r: Resource) -> bool:
    value = value.strip()
    if not value:
        return False
    names = {r.name, r.attributes.get("arn", "")}
    if r.kind == "secret":
        names.add(SECRET_SUFFIX_RE.sub("", r.name))
    if value in names:
        return True
    return "*" in r.name and fnmatch.fnmatchcase(value, r.name)


def mark_env_references(g: DeployGraph) -> None:
    """Record which workload setting names a cloud resource (``Deployment/x:S3_BUCKET``). Only the
    env *name* is written; the compared value already is the resource name (or is not copied)."""
    for r in g.resources:
        if r.kind not in {"s3_bucket", "secret", "kms_key"}:
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


class FileAdapter:
    """Reads a directory of exports. Expected files (any may be absent):

    deployments.json · serviceaccounts.json · clusters.json (CNPG) · aws.json
    ``aws.json``: {"roles": {arn: {"policies": [{"name":..., "statements":[...]}]}},
                   "buckets": {name: {"public": bool}}, "secrets": {name: {...}}, "kms": {id: {...}}}
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.read: list[str] = []

    def _load(self, name: str) -> Any:
        p = self.dir / name
        if not p.is_file():
            return None
        self.read.append(f"file:{name}")
        return json.loads(p.read_text(encoding="utf-8"))

    def trace(self, namespace: str) -> DeployGraph:
        g = DeployGraph(namespace)
        self.read = []
        deployments = [d for d in _items(self._load("deployments.json")) if d.get("metadata", {}).get("namespace", namespace) == namespace]
        sas = {sa["metadata"]["name"]: sa for sa in _items(self._load("serviceaccounts.json")) if sa.get("metadata", {}).get("namespace", namespace) == namespace}
        aws = self._load("aws.json") or {}

        def follow_role(sa_name: str, via: str) -> None:
            role_arn = sas.get(sa_name, {}).get("metadata", {}).get("annotations", {}).get(IRSA_ANNOTATION)
            if not role_arn:
                g.notes.append(f"{via}: IRSA 역할 주석 없음")
                return
            g.resources.append(Resource("iam_role", role_arn, "iam", via, [], {}))
            for pol in aws.get("roles", {}).get(role_arn, {}).get("policies", []):
                g.resources.extend(_policy_resources(pol.get("statements", []), f"{via} → IAMRole → policy {pol.get('name', '?')}"))

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
            follow_role(sa_name, f"Deployment/{d['metadata']['name']} → ServiceAccount/{sa_name}")
        # A cloud identity in the namespace that no Deployment uses still reaches cloud resources
        # for it (ESO SecretStore, operator jobs). Its grants are deploy-side dependencies too.
        for sa_name in sorted(set(sas) - used):
            if IRSA_ANNOTATION in sas[sa_name].get("metadata", {}).get("annotations", {}):
                follow_role(sa_name, f"ServiceAccount/{sa_name} (Deployment 미사용: SecretStore · 운영 작업 등)")
        for cl in _items(self._load("clusters.json")):
            if cl.get("metadata", {}).get("namespace", namespace) == namespace:
                g.resources.append(Resource("cnpg_cluster", cl["metadata"]["name"], "db", f"Namespace/{namespace}", [], {"instances": str(cl.get("spec", {}).get("instances", "")), "wal_level": str(cl.get("spec", {}).get("postgresql", {}).get("parameters", {}).get("wal_level", ""))}))
        g.resources = _merge_resources(g.resources)
        # enrich with bucket/secret/kms attributes from fixture (public exposure is a gate input, not decided here)
        for r in g.resources:
            section = {"s3_bucket": "buckets", "secret": "secrets", "kms_key": "kms"}.get(r.kind)
            if section and r.name in aws.get(section, {}):
                allowed = RESOURCE_ATTRIBUTES[r.kind]
                r.attributes.update({k: str(v) for k, v in aws[section][r.name].items() if k in allowed})
        mark_env_references(g)
        g.api_calls = list(self.read)  # only files that existed and were read
        return g


class Boto3Adapter:
    """Live, read-only AWS tracing. Kubernetes objects still come from exports (kubeconfig is
    not handed to this module). Every API call is appended to ``api_calls``; anything outside
    READ_ONLY_API raises before the call is made.
    """

    def __init__(self, directory: Path, session: Any = None):
        self.files = FileAdapter(directory)
        if session is None:  # pragma: no cover - needs the aws extra
            import boto3

            session = boto3.Session()
        self.session = session
        self.calls: list[str] = []

    def _call(self, client: Any, api: str, **kwargs: Any) -> Any:
        if api not in READ_ONLY_API:
            raise PermissionError(f"write or unlisted API refused by T16 adapter: {api}")
        self.calls.append(api)
        method = api.split(":", 1)[1]
        method = re.sub(r"(?<!^)(?=[A-Z])", "_", method).lower()
        return getattr(client, method)(**kwargs)

    def trace(self, namespace: str) -> DeployGraph:
        g = self.files.trace(namespace)
        iam = self.session.client("iam")
        for r in [x for x in g.resources if x.kind == "iam_role"]:
            role_name = r.name.rsplit("/", 1)[-1]
            attached = self._call(iam, "iam:ListAttachedRolePolicies", RoleName=role_name)
            for p in attached.get("AttachedPolicies", []):
                pol = self._call(iam, "iam:GetPolicy", PolicyArn=p["PolicyArn"])["Policy"]
                ver = self._call(iam, "iam:GetPolicyVersion", PolicyArn=p["PolicyArn"], VersionId=pol["DefaultVersionId"])
                stmts = ver["PolicyVersion"]["Document"].get("Statement", [])
                g.resources.extend(_policy_resources(stmts if isinstance(stmts, list) else [stmts], f"{r.via} → IAMRole → attached {p['PolicyName']}"))
            inline = self._call(iam, "iam:ListRolePolicies", RoleName=role_name)
            for name in inline.get("PolicyNames", []):
                doc = self._call(iam, "iam:GetRolePolicy", RoleName=role_name, PolicyName=name)["PolicyDocument"]
                stmts = doc.get("Statement", [])
                g.resources.extend(_policy_resources(stmts if isinstance(stmts, list) else [stmts], f"{r.via} → IAMRole → inline {name}"))
        g.resources = _merge_resources(g.resources)
        self._describe(g.resources)
        mark_env_references(g)
        g.api_calls = sorted(set(g.api_calls + self.calls))
        return g

    def _observe(self, client: Any, api: str, **kwargs: Any) -> Any:
        """A read that may fail (missing permission, deleted resource): the failure is data."""
        try:
            return self._call(client, api, **kwargs)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - observation failure is data, not a crash
            return exc

    def _describe(self, resources: list[Resource]) -> None:
        """Bucket exposure/region, secret rotation/encrypting key, KMS key state. Each distinct
        resource is read once; only RESOURCE_ATTRIBUTES are copied. Wildcard names are skipped."""
        clients: dict[tuple[str, str | None], Any] = {}

        def client(service: str, region: str | None) -> Any:
            if (service, region) not in clients:
                clients[(service, region)] = self.session.client(service, region_name=region) if region else self.session.client(service)
            return clients[(service, region)]

        seen: dict[tuple[str, str], dict[str, str]] = {}
        for r in resources:
            if r.kind not in RESOURCE_ATTRIBUTES or "*" in r.name:
                continue
            key = (r.kind, r.name)
            if key not in seen:
                seen[key] = self._read_attributes(r, client)
            r.attributes.update(seen[key])

    def _read_attributes(self, r: Resource, client: Any) -> dict[str, str]:
        arn = r.attributes.get("arn", "")
        region = arn.split(":")[3] if arn.count(":") >= 5 and arn.split(":")[3] else None
        out: dict[str, str] = {}
        if r.kind == "s3_bucket":
            s3 = client("s3", None)
            status = self._observe(s3, "s3:GetBucketPolicyStatus", Bucket=r.name)
            out["public"] = f"UNKNOWN: {type(status).__name__}" if isinstance(status, Exception) else str(bool(status.get("PolicyStatus", {}).get("IsPublic", False)))
            loc = self._observe(s3, "s3:GetBucketLocation", Bucket=r.name)
            out["region"] = f"UNKNOWN: {type(loc).__name__}" if isinstance(loc, Exception) else (loc.get("LocationConstraint") or "us-east-1")
        elif r.kind == "secret":
            desc = self._observe(client("secretsmanager", region), "secretsmanager:DescribeSecret", SecretId=arn or r.name)
            if isinstance(desc, Exception):
                out["rotation_enabled"] = f"UNKNOWN: {type(desc).__name__}"
            else:
                out["rotation_enabled"] = str(bool(desc.get("RotationEnabled", False)))
                out["kms_key_id"] = str(desc.get("KmsKeyId") or "aws-managed (aws/secretsmanager)")
        elif r.kind == "kms_key":
            desc = self._observe(client("kms", region), "kms:DescribeKey", KeyId=arn or r.name)
            if isinstance(desc, Exception):
                out["key_state"] = f"UNKNOWN: {type(desc).__name__}"
            else:
                meta = desc.get("KeyMetadata", {})
                out.update({"key_manager": str(meta.get("KeyManager", "")), "key_state": str(meta.get("KeyState", "")), "key_spec": str(meta.get("KeySpec", ""))})
        return {k: v for k, v in out.items() if k in RESOURCE_ATTRIBUTES[r.kind]}
