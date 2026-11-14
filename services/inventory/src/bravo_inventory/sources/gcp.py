"""GCP source deploy trace: ServiceAccount ``iam.gke.io/gcp-service-account`` (GKE Workload
Identity) → Google service account → IAM policy bindings → GCS buckets · Secret Manager
secrets · Cloud KMS keys (ADR-0034 ``source.deploy_trace`` for ``gcp``).

* ``GcpFileAdapter`` reads ``kubectl get -o json`` exports and a ``gcp.json`` export.
* ``GcloudAdapter``  builds the same ``gcp.json`` shape with read-only ``gcloud ... --format=json``
  subprocess calls from a fixed allowlist and records each call.

``gcp.json``::

    {"project_id": "...",
     "project_policy": {"bindings": [{"role": "roles/...", "members": ["serviceAccount:..."]}]},
     "service_accounts": {"<gsa email>": {"iam_policy": {"bindings": [...]}}},
     "resource_policies": [{"resource": "//storage.googleapis.com/projects/_/buckets/<b>",
                            "policy": {"bindings": [...]}}],
     "buckets": {"<b>": {...}}, "secrets": {"<id>": {...}}, "kms_keys": {"<key path>": {...}}}

``resource_policies`` is the shape of ``gcloud asset search-all-iam-policies``. An optional
``project_number`` lets a KSA without the annotation be traced as a direct Workload Identity
principal (``principal://.../subject/ns/<ns>/sa/<ksa>``). ``secrets[<id>].kms_key_name`` (CMEK)
adds the key behind a reached secret even when the app identity has no KMS binding.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

WORKLOAD_IDENTITY_ANNOTATION = "iam.gke.io/gcp-service-account"
WORKLOAD_IDENTITY_USER = "roles/iam.workloadIdentityUser"
PUBLIC_MEMBERS = frozenset({"allUsers", "allAuthenticatedUsers"})

RESOURCE_ATTRIBUTES: dict[str, frozenset[str]] = {
    "gcs_bucket": frozenset({"public", "location", "versioning", "retention_policy", "uniform_access"}),
    "gcp_secret": frozenset({"replication", "rotation", "kms_key_name"}),
    "gcp_kms_key": frozenset({"purpose", "state", "protection_level", "rotation_period"}),
}
SECTIONS = {"gcs_bucket": "buckets", "gcp_secret": "secrets", "gcp_kms_key": "kms_keys"}

# Full resource name (Cloud Asset form) → (kind, role, name).
_RESOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # IAM policy form ``projects/_/buckets/<b>``; Cloud Asset search returns ``//storage.googleapis.com/<b>``
    # (A11 live readback), so both are buckets.
    (re.compile(r"^//storage\.googleapis\.com/projects/_/buckets/([^/]+)$"), "gcs_bucket", "object_store"),
    (re.compile(r"^//storage\.googleapis\.com/([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])$"), "gcs_bucket", "object_store"),
    (re.compile(r"^//secretmanager\.googleapis\.com/projects/[^/]+/secrets/([^/]+)$"), "gcp_secret", "secrets"),
    (re.compile(r"^//cloudkms\.googleapis\.com/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+)$"), "gcp_kms_key", "kms"),
)
# Project-level role prefix → platform role. A project binding of a storage / secret / KMS role
# reaches every resource of that service: it is recorded as a wildcard (agent must classify).
_PROJECT_ROLE_PREFIX: tuple[tuple[str, str], ...] = (
    ("roles/storage.", "object_store"),
    ("roles/secretmanager.", "secrets"),
    ("roles/cloudkms.", "kms"),
    ("roles/logging.", "audit"),
    ("roles/monitoring.", "observability"),
)
_BROAD_ROLES = frozenset({"roles/owner", "roles/editor", "roles/viewer"})


def gsa_email(sa: dict) -> str | None:
    return annotations(sa).get(WORKLOAD_IDENTITY_ANNOTATION)


def parse_resource_name(full_name: str) -> tuple[str, str, str] | None:
    for pattern, kind, role in _RESOURCE_PATTERNS:
        m = pattern.match(full_name)
        if m:
            return kind, role, m.group(1)
    return None


def _bindings(policy: Any) -> list[dict]:
    return [b for b in (policy or {}).get("bindings", []) if isinstance(b, dict)]


def _roles_for(policy: Any, member: str) -> list[str]:
    return sorted({b.get("role", "") for b in _bindings(policy) if member in b.get("members", [])} - {""})


def project_of(email: str) -> str | None:
    m = re.fullmatch(r"[^@]+@([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com", email)
    return m.group(1) if m else None


def member_resources(member: str, data: dict, via: str) -> list[Resource]:
    """What the IAM ``member`` (``serviceAccount:...`` or a ``principal://`` Workload Identity
    principal) reaches through IAM bindings in ``data``."""
    out: list[Resource] = []
    for role in _roles_for(data.get("project_policy"), member):
        scope = next((r for prefix, r in _PROJECT_ROLE_PREFIX if role.startswith(prefix)), None)
        if role in _BROAD_ROLES or scope in {"object_store", "secrets", "kms"}:
            out.append(Resource("wildcard", "*", "iam", f"{via} → project binding {role}", [role],
                                {"warning": "project-level binding reaches every resource", "scope": "project"}))
        else:
            out.append(Resource("gcp_project_role", role, scope or "iam", f"{via} → project binding", [role], {"scope": "project"}))
    for entry in data.get("resource_policies", []):
        full = str(entry.get("resource", ""))
        roles = _roles_for(entry.get("policy"), member)
        if not roles or full.startswith("//cloudresourcemanager.googleapis.com/projects/"):
            continue  # the project policy is read once as ``project_policy``
        parsed = parse_resource_name(full)
        if parsed is None:
            out.append(Resource("gcp_resource", full, "unknown", f"{via} → binding", roles, {"resource": full}))
            continue
        kind, role, name = parsed
        attrs = {"resource": full}
        if kind == "gcs_bucket":
            public = any(m in PUBLIC_MEMBERS for b in _bindings(entry.get("policy")) for m in b.get("members", []))
            attrs["public"] = str(public)
        out.append(Resource(kind, name, role, f"{via} → binding {', '.join(roles)}", roles, attrs))
    return out


def gsa_resources(email: str, data: dict, via: str) -> list[Resource]:
    """What the Google service account ``email`` reaches through IAM bindings in ``data``."""
    return member_resources(f"serviceAccount:{email}", data, via)


def wi_principal(project_number: str, project_id: str, namespace: str, ksa: str) -> str:
    """Workload Identity Federation for GKE principal of a KSA (direct grant, no GSA)."""
    return (f"principal://iam.googleapis.com/projects/{project_number}/locations/global/"
            f"workloadIdentityPools/{project_id}.svc.id.goog/subject/ns/{namespace}/sa/{ksa}")


def cmek_keys(resources: list[Resource], data: dict) -> list[Resource]:
    """Cloud KMS keys that encrypt a reached Secret (CMEK). The app identity needs no KMS
    binding for this (the Secret Manager service agent holds it), yet the key is a deploy
    dependency: a target must provide an equivalent key."""
    out = []
    for r in resources:
        if r.kind != "gcp_secret":
            continue
        names = str(data.get("secrets", {}).get(r.name, {}).get("kms_key_name") or "")
        for key in filter(None, names.split(",")):
            out.append(Resource("gcp_kms_key", key, "kms", f"{r.via} → Secret/{r.name} CMEK", [], {"reached_by": "cmek"}))
    return out


class GcpFileAdapter:
    """Reads deployments.json · serviceaccounts.json · clusters.json · gcp.json from a directory."""

    kind = "gcp"

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.calls: list[str] = []

    def _gcp_data(self, reader: ExportReader, namespace: str, emails: list[str], ksas: list[str]) -> dict:
        return reader.load("gcp.json") or {}

    def trace(self, namespace: str) -> DeployGraph:
        reader = ExportReader(self.dir)
        self.calls = []
        pending: list[tuple[str, str, str]] = []  # (ksa, gsa email, via)
        direct: list[tuple[str, str]] = []  # (ksa without annotation, via)

        def follow(g: DeployGraph, sa_name: str, sa: dict, via: str) -> None:
            email = gsa_email(sa)
            if not email:
                direct.append((sa_name, via))
                return
            g.resources.append(Resource("gcp_service_account", email, "iam", via, [], {}))
            pending.append((sa_name, email, f"{via} → GSA/{email}"))

        g = walk_namespace(reader, namespace, self.kind, follow, lambda _n, sa: gsa_email(sa) is not None)
        data = self._gcp_data(reader, namespace, sorted({e for _, e, _ in pending}), sorted({k for k, _ in direct}))
        project = data.get("project_id")
        number = data.get("project_number")
        for ksa, email, via in pending:
            g.resources.extend(gsa_resources(email, data, via))
            sa_policy = data.get("service_accounts", {}).get(email, {}).get("iam_policy")
            if sa_policy is not None:
                wi_member = f"serviceAccount:{project or project_of(email) or '?'}.svc.id.goog[{namespace}/{ksa}]"
                if WORKLOAD_IDENTITY_USER not in _roles_for(sa_policy, wi_member):
                    g.notes.append(f"{via}: {WORKLOAD_IDENTITY_USER} 바인딩이 없어 이 KSA가 GSA를 쓰지 못한다")
        for ksa, via in direct:
            found: list[Resource] = []
            if project and number:
                member = wi_principal(str(number), project, namespace, ksa)
                found = member_resources(member, data, f"{via} → WI principal")
            if found:
                g.resources.append(Resource("gcp_wi_principal", f"ns/{namespace}/sa/{ksa}", "iam", via, [], {}))
                g.resources.extend(found)
            else:
                g.notes.append(f"{via}: Workload Identity 주석({WORKLOAD_IDENTITY_ANNOTATION})도 직접 WI principal 권한도 없음")
        g.resources.extend(cmek_keys(g.resources, data))
        g.resources = merge_resources(g.resources)
        apply_attributes(g.resources, data, SECTIONS, RESOURCE_ATTRIBUTES)
        mark_env_references(g)
        g.api_calls = sorted(set(reader.read + self.calls))
        return g


# Read-only gcloud command prefixes. Anything else is refused before a process is started.
GCLOUD_READ_ONLY: tuple[tuple[str, ...], ...] = (
    ("projects", "get-iam-policy"),
    ("projects", "describe"),
    ("iam", "service-accounts", "get-iam-policy"),
    ("asset", "search-all-iam-policies"),
    ("secrets", "describe"),
    ("storage", "buckets", "describe"),
    ("kms", "keys", "describe"),
)


def _run_gcloud(argv: list[str]) -> str:  # pragma: no cover - needs gcloud and credentials
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=120).stdout


def _secret_attributes(secret: dict) -> dict:
    replication = secret.get("replication") or {}
    if "automatic" in replication:
        configs, mode = [replication.get("automatic") or {}], "automatic"
    else:
        configs, mode = (replication.get("userManaged") or {}).get("replicas") or [], "user-managed"
    keys = sorted({k for c in configs if (k := (c.get("customerManagedEncryption") or {}).get("kmsKeyName"))})
    attrs = {"replication": mode, "rotation": bool(secret.get("rotation"))}
    if keys:
        attrs["kms_key_name"] = ",".join(keys)
    return attrs


def _bucket_attributes(bucket: dict) -> dict:
    return {
        "location": bucket.get("location"),
        "versioning": bool(bucket.get("versioning_enabled")),
        "uniform_access": bool(bucket.get("uniform_bucket_level_access")),
        "retention_policy": bool(bucket.get("retention_policy")),
    }


def _kms_attributes(key: dict) -> dict:
    primary = key.get("primary") or {}
    return {
        "purpose": key.get("purpose"),
        "state": primary.get("state"),
        "protection_level": primary.get("protectionLevel") or (key.get("versionTemplate") or {}).get("protectionLevel"),
        "rotation_period": key.get("rotationPeriod"),
    }


class GcloudAdapter(GcpFileAdapter):
    """Live, read-only GCP tracing. Kubernetes objects still come from exports. Each gcloud call
    is checked against ``GCLOUD_READ_ONLY`` and recorded in ``api_calls`` as ``gcloud <command>``.

    Besides annotated KSAs (KSA → GSA), KSAs without the annotation are traced as direct
    Workload Identity principals. Reached buckets, secrets and keys are described (metadata
    only, never secret payloads or objects) so CMEK keys behind a secret are found too."""

    def __init__(self, directory: Path, project_id: str | None = None, run: Callable[[list[str]], str] | None = None):
        super().__init__(directory)
        self.project_id = project_id
        self.run = run or _run_gcloud

    def gcloud(self, *args: str) -> Any:
        prefix = next((p for p in GCLOUD_READ_ONLY if tuple(args[: len(p)]) == p), None)
        if prefix is None:
            raise PermissionError(f"write or unlisted gcloud command refused by A3 adapter: {' '.join(args[:3])}")
        self.calls.append("gcloud " + " ".join(prefix))
        out = self.run(["gcloud", *args, "--format=json"])
        return json.loads(out) if out.strip() else None

    def _search(self, project: str, member: str) -> list[dict]:
        found = self.gcloud("asset", "search-all-iam-policies", f"--scope=projects/{project}", f"--query=policy:\"{member}\"")
        return [e for e in (found or []) if isinstance(e, dict)]

    def _gcp_data(self, reader: ExportReader, namespace: str, emails: list[str], ksas: list[str]) -> dict:
        project = self.project_id or next((p for e in emails if (p := project_of(e))), None)
        data: dict[str, Any] = {"project_id": project, "service_accounts": {}, "resource_policies": []}
        if not (emails or ksas) or not project:
            return data
        data["project_policy"] = self.gcloud("projects", "get-iam-policy", project)
        members = [f"serviceAccount:{e}" for e in emails]
        for email in emails:
            data["service_accounts"][email] = {"iam_policy": self.gcloud("iam", "service-accounts", "get-iam-policy", email)}
        if ksas:
            data["project_number"] = str((self.gcloud("projects", "describe", project) or {}).get("projectNumber", ""))
            if data["project_number"]:
                members += [wi_principal(data["project_number"], project, namespace, k) for k in ksas]
        # Keep every result: a member-filtered search returns only that member's bindings of a
        # policy, so two searches can return different halves of the same resource's policy.
        for member in members:
            data["resource_policies"].extend(self._search(project, member))
        reached = [r for m in members for r in member_resources(m, data, "")]
        data["buckets"], data["secrets"], data["kms_keys"] = {}, {}, {}
        for r in reached:
            if r.kind == "gcs_bucket" and r.name not in data["buckets"]:
                data["buckets"][r.name] = _bucket_attributes(self.gcloud("storage", "buckets", "describe", f"gs://{r.name}") or {})
            elif r.kind == "gcp_secret" and r.name not in data["secrets"]:
                data["secrets"][r.name] = _secret_attributes(self.gcloud("secrets", "describe", r.name, f"--project={project}") or {})
        keys = {r.name for r in reached if r.kind == "gcp_kms_key"}
        keys |= {k for s in data["secrets"].values() for k in str(s.get("kms_key_name") or "").split(",") if k}
        for key in sorted(keys):
            data["kms_keys"][key] = _kms_attributes(self.gcloud("kms", "keys", "describe", key) or {})
        return data
