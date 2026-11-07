"""AWS source deploy trace: ServiceAccount IRSA annotation → IAM role → policy statements →
S3 buckets · Secrets Manager entries · KMS keys (ADR-0034 ``source.deploy_trace`` for ``aws``).

* ``FileAdapter``  reads ``kubectl get -o json`` exports and an ``aws.json`` fixture.
* ``Boto3Adapter`` (optional extra ``aws``) calls a fixed allowlist of read-only APIs and
  records every call so the inventory can prove "no write API was used" (T16 할 일).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..catalog import role_for_service
from ..deploy_trace import (
    DeployGraph,
    ExportReader,
    Resource,
    annotations,
    apply_attributes,
    mark_env_references,
    merge_resources,
    walk_namespace,
)

# Attributes copied from the cloud readback into the inventory. Anything else observed about a
# resource stays out of the document (the inventory is shared with the console and the model).
RESOURCE_ATTRIBUTES: dict[str, frozenset[str]] = {
    "s3_bucket": frozenset({"public", "region", "versioning", "object_lock"}),
    "secret": frozenset({"rotation_enabled", "kms_key_id"}),
    "kms_key": frozenset({"key_manager", "key_state", "key_spec"}),
}
SECTIONS = {"s3_bucket": "buckets", "secret": "secrets", "kms_key": "kms"}

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


def irsa_role(sa: dict) -> str | None:
    return annotations(sa).get(IRSA_ANNOTATION)


def policy_resources(statements: list[dict], via: str) -> list[Resource]:
    """IAM policy statements → the S3 · Secrets Manager · KMS resources they allow."""
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


_policy_resources = policy_resources  # T16 name


class FileAdapter:
    """Reads a directory of exports. Expected files (any may be absent):

    deployments.json · serviceaccounts.json · clusters.json (CNPG) · aws.json
    ``aws.json``: {"roles": {arn: {"policies": [{"name":..., "statements":[...]}]}},
                   "buckets": {name: {"public": bool}}, "secrets": {name: {...}}, "kms": {id: {...}}}
    """

    kind = "aws"

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.read: list[str] = []

    def trace(self, namespace: str) -> DeployGraph:
        reader = ExportReader(self.dir)
        aws: dict = {}

        def follow(g: DeployGraph, sa_name: str, sa: dict, via: str) -> None:
            role_arn = irsa_role(sa)
            if not role_arn:
                g.notes.append(f"{via}: IRSA 역할 주석 없음")
                return
            g.resources.append(Resource("iam_role", role_arn, "iam", via, [], {}))
            for pol in aws.get("roles", {}).get(role_arn, {}).get("policies", []):
                g.resources.extend(policy_resources(pol.get("statements", []), f"{via} → IAMRole → policy {pol.get('name', '?')}"))

        aws.update(reader.load("aws.json") or {})
        g = walk_namespace(reader, namespace, self.kind, follow, lambda _n, sa: irsa_role(sa) is not None)
        g.resources = merge_resources(g.resources)
        # enrich with bucket/secret/kms attributes from fixture (public exposure is a gate input, not decided here)
        apply_attributes(g.resources, aws, SECTIONS, RESOURCE_ATTRIBUTES)
        mark_env_references(g)
        self.read = list(reader.read)
        g.api_calls = list(reader.read)  # only files that existed and were read
        return g


AwsFileAdapter = FileAdapter


class Boto3Adapter:
    """Live, read-only AWS tracing. Kubernetes objects still come from exports (kubeconfig is
    not handed to this module). Every API call is appended to ``api_calls``; anything outside
    READ_ONLY_API raises before the call is made.
    """

    kind = "aws"

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
                g.resources.extend(policy_resources(stmts if isinstance(stmts, list) else [stmts], f"{r.via} → IAMRole → attached {p['PolicyName']}"))
            inline = self._call(iam, "iam:ListRolePolicies", RoleName=role_name)
            for name in inline.get("PolicyNames", []):
                doc = self._call(iam, "iam:GetRolePolicy", RoleName=role_name, PolicyName=name)["PolicyDocument"]
                stmts = doc.get("Statement", [])
                g.resources.extend(policy_resources(stmts if isinstance(stmts, list) else [stmts], f"{r.via} → IAMRole → inline {name}"))
        g.resources = merge_resources(g.resources)
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
