"""Slot `target.neutral_apply`: neutral model → target object exposure and secret delivery.

Renderers are deterministic and pure: the same model and target context always give the same
bytes, so a rehearsal-verified render and the one applied later share one digest. They never
grant an ``unresolved`` principal (ADR-0034 §3.2.4) and never invent credentials. Whatever
they could not render with the same meaning is listed in ``skipped`` (not granted) or
``notes`` (rendered with a documented difference) for the item's record.

Workload → target principal is a target-side fact (IRSA role, GKE Workload Identity member,
SeaweedFS identity name) supplied by the caller as ``principal_for``. ``None`` means the
workload has no identity on the target yet: it is skipped, not granted something else.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .export import S3_ARN
from .models import Anonymous, NeutralError, ObjectAccessPolicy, SecretBinding, Unresolved, Workload

PrincipalFor = Callable[[Workload], str | None]


@dataclass(frozen=True)
class Rendered:
    kind: str
    document: object
    skipped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def text(self) -> str:
        """The exact file content. SeaweedFS uses the serialisation of T9 ``object_exposure.py``."""
        return json.dumps(self.document, indent=2) + "\n"

    def digest(self) -> str:
        """Same form as apply-path ``policy_digest`` (sha256 of the UTF-8 file content)."""
        return "sha256:" + hashlib.sha256(self.text().encode("utf-8")).hexdigest()


def _skip(grant) -> str:
    principal = grant.principal
    if isinstance(principal, Unresolved):
        return f"unresolved:{principal.ref}"
    return f"no_target_identity:{principal.namespace}/{principal.service_account}"


# --- onprem: SeaweedFS s3.json ----------------------------------------------------------------------

SEAWEED_PRESERVED = ("Tagging",)  # not in the neutral model; kept on identities the policy manages


def _seaweed_actions(actions: tuple[str, ...]) -> tuple[list[str], bool]:
    """SeaweedFS ``Write`` covers put and delete; returns (actions, widened)."""
    out = []
    if "read" in actions:
        out.append("Read")
    if "write" in actions or "delete" in actions:
        out.append("Write")
    if "list" in actions:
        out.append("List")
    return out, ("write" in actions) != ("delete" in actions)


def render_seaweedfs_identities(policy: ObjectAccessPolicy, *, principal_for: PrincipalFor | None = None,
                                base: Mapping | None = None, bucket: str | None = None) -> Rendered:
    """New ``s3.json`` for ``bucket`` on top of ``base`` (the current file).

    A bucket policy is additive to identity permissions, so identities the policy does not name
    keep their actions unchanged. Identities it names (``anonymous`` and mapped workloads) get
    exactly the rendered actions on this bucket; their actions on other buckets stay. Existing
    credentials are carried over as they are, never created: a workload whose identity is not in
    ``base`` is skipped."""
    bucket = bucket or policy.bucket
    principal_for = principal_for or (lambda w: None)
    identities = [dict(i) for i in (base or {"identities": []}).get("identities", [])]
    if not all(isinstance(i.get("name"), str) and isinstance(i.get("actions", []), list) for i in identities):
        raise NeutralError("TARGET_AUTH_UNRECOGNIZED")
    by_name = {i["name"]: i for i in identities}
    if len(by_name) != len(identities):
        raise NeutralError("TARGET_AUTH_UNRECOGNIZED")
    wanted: dict[str, list[str]] = {}
    skipped, notes = [], []
    if policy.tls_required:
        notes.append("tls_required_not_expressible")
    for grant in policy.grants:
        if isinstance(grant.principal, Unresolved):
            skipped.append(_skip(grant))
            continue
        name = "anonymous" if isinstance(grant.principal, Anonymous) else principal_for(grant.principal)
        if name is None or (name != "anonymous" and name not in by_name):
            skipped.append(_skip(grant))
            continue
        actions, widened = _seaweed_actions(grant.actions)
        if widened:
            notes.append(f"write_implies_delete:{name}")
        scope = f"{bucket}/{grant.prefix}" if grant.prefix else bucket
        wanted.setdefault(name, []).extend(f"{a}:{scope}" for a in actions)
    managed = set(wanted) | {"anonymous"}
    out = []
    for identity in identities:
        name = identity["name"]
        if name not in managed:
            out.append(identity)
            continue
        kept = []
        for entry in identity.get("actions", []):
            action, _, target = entry.partition(":")
            if not target:
                raise NeutralError("TARGET_GLOBAL_GRANT")  # an unscoped action would outlive the policy
            if target == bucket or target.startswith(bucket + "/"):
                if action in SEAWEED_PRESERVED:
                    kept.append(entry)
                continue
            kept.append(entry)
        actions = _dedupe(wanted.pop(name, []) + kept, bucket)
        if not actions and "credentials" not in identity:
            continue  # an anonymous identity with nothing left is removed
        out.append({**identity, "actions": actions})
    if "anonymous" in wanted:
        out.append({"name": "anonymous", "actions": _dedupe(wanted.pop("anonymous"), bucket)})
    return Rendered("onprem", {"identities": out}, tuple(skipped), tuple(notes))


def _dedupe(entries: list[str], bucket: str) -> list[str]:
    order = {"Admin": 0, "Read": 1, "Write": 2, "List": 3, "Tagging": 4}
    seen = list(dict.fromkeys(entries))
    ours = [e for e in seen if e.partition(":")[2].split("/")[0] == bucket]
    others = [e for e in seen if e not in ours]
    return sorted(ours, key=lambda e: (e.partition(":")[2], order.get(e.partition(":")[0], 9))) + others


# --- aws: S3 bucket policy ------------------------------------------------------------------------

S3_OBJECT = {"read": "s3:GetObject", "write": "s3:PutObject", "delete": "s3:DeleteObject"}
IAM_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")


def render_s3_bucket_policy(policy: ObjectAccessPolicy, *, principal_for: PrincipalFor | None = None,
                            bucket: str | None = None) -> Rendered:
    """S3 bucket policy. ``principal_for`` returns the IRSA role ARN of a workload.

    Without public read the policy carries an explicit anonymous-read Deny (the shape the
    rehearsal and apply-path already accept); ``tls_required`` adds the SecureTransport Deny."""
    bucket = bucket or policy.bucket
    principal_for = principal_for or (lambda w: None)
    arn, objects = f"{S3_ARN}{bucket}", f"{S3_ARN}{bucket}/"
    statements, skipped = [], []
    for grant in policy.grants:
        if isinstance(grant.principal, Unresolved):
            skipped.append(_skip(grant))
            continue
        if isinstance(grant.principal, Anonymous):
            principal, sid = "*", "Public"
        else:
            role = principal_for(grant.principal)
            if role is None:
                skipped.append(_skip(grant))
                continue
            if not IAM_ROLE_ARN.fullmatch(role):
                raise NeutralError("INVALID_TARGET_PRINCIPAL")
            principal = {"AWS": role}
            sid = _sid(grant.principal.namespace, grant.principal.service_account)
        object_actions = [S3_OBJECT[a] for a in ("read", "write", "delete") if a in grant.actions]
        if object_actions:
            statements.append({"Sid": f"{sid}Objects{_sid(grant.prefix)}", "Effect": "Allow", "Principal": principal,
                               "Action": object_actions[0] if len(object_actions) == 1 else object_actions,
                               "Resource": f"{objects}{grant.prefix}*"})
        if "list" in grant.actions:
            st = {"Sid": f"{sid}List{_sid(grant.prefix)}", "Effect": "Allow", "Principal": principal,
                  "Action": "s3:ListBucket", "Resource": arn}
            if grant.prefix:
                st["Condition"] = {"StringLike": {"s3:prefix": f"{grant.prefix}*"}}
            statements.append(st)
    if not policy.public_read:
        statements.append({"Sid": "DenyAnonymousRead", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                           "Resource": f"{objects}*",
                           "Condition": {"StringEquals": {"aws:PrincipalType": "Anonymous"}}})
    if policy.tls_required:
        statements.append({"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                           "Resource": [arn, f"{objects}*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}})
    return Rendered("aws", {"Version": "2012-10-17", "Statement": statements}, tuple(skipped))


def _sid(*parts: str) -> str:
    return "".join(p.title() for part in parts for p in re.split(r"[^A-Za-z0-9]+", part) if p)


# --- gcp: GCS bucket IAM policy -------------------------------------------------------------------

MEMBER_RE = re.compile(r"^(principal://|principalSet://|serviceAccount:)[A-Za-z0-9@._:/\[\]-]{1,400}$")


def _gcs_roles(actions: tuple[str, ...]) -> tuple[list[str], bool]:
    """Smallest predefined roles covering ``actions``; (roles, widened)."""
    a = set(actions)
    if "delete" in a:
        return ["roles/storage.objectUser"], a != {"read", "write", "list", "delete"}
    roles = []
    if {"read", "list"} <= a:
        roles.append("roles/storage.objectViewer")
    elif "read" in a:
        roles.append("roles/storage.legacyObjectReader")
    elif "list" in a:
        roles.append("roles/storage.legacyBucketReader")
    if "write" in a:
        roles.append("roles/storage.objectCreator")
    return roles, False


def render_gcs_iam_policy(policy: ObjectAccessPolicy, *, principal_for: PrincipalFor | None = None,
                          bucket: str | None = None) -> Rendered:
    """GCS bucket IAM policy (no ``etag``: the applier reads the current one). ``principal_for``
    returns the workload's IAM member, e.g. ``gke_workload_principal(...)``. A prefix becomes an
    IAM condition (needs uniform bucket-level access)."""
    bucket = bucket or policy.bucket
    principal_for = principal_for or (lambda w: None)
    bindings: dict[tuple[str, str], set[str]] = {}
    skipped, notes = [], []
    if not policy.tls_required:
        notes.append("tls_always_required_by_target")
    for grant in policy.grants:
        if isinstance(grant.principal, Unresolved):
            skipped.append(_skip(grant))
            continue
        if isinstance(grant.principal, Anonymous):
            member = "allUsers"
        else:
            member = principal_for(grant.principal)
            if member is None:
                skipped.append(_skip(grant))
                continue
            if not MEMBER_RE.fullmatch(member):
                raise NeutralError("INVALID_TARGET_PRINCIPAL")
        roles, widened = _gcs_roles(grant.actions)
        if widened:
            notes.append(f"role_widened:{member if member == 'allUsers' else 'workload'}")
        for role in roles:
            bindings.setdefault((role, grant.prefix), set()).add(member)
    out = []
    for (role, prefix), members in sorted(bindings.items()):
        binding = {"role": role, "members": sorted(members)}
        if prefix:
            binding["condition"] = {
                "title": f"prefix-{_sid(prefix).lower()}"[:100],
                "expression": f'resource.name.startsWith("projects/_/buckets/{bucket}/objects/{prefix}")'}
        out.append(binding)
    version = 3 if any("condition" in b for b in out) else 1
    return Rendered("gcp", {"version": version, "bindings": out}, tuple(skipped), tuple(notes))


def gke_workload_principal(project_number: str, project_id: str) -> PrincipalFor:
    """Workload Identity Federation for GKE: the Kubernetes service account itself is the member."""
    if not re.fullmatch(r"[0-9]{1,20}", project_number) or not re.fullmatch(r"[a-z][-a-z0-9]{4,28}[a-z0-9]", project_id):
        raise NeutralError("INVALID_TARGET_CONTEXT")
    return lambda w: (f"principal://iam.googleapis.com/projects/{project_number}/locations/global/"
                      f"workloadIdentityPools/{project_id}.svc.id.goog/subject/ns/{w.namespace}/sa/{w.service_account}")


def mapping(table: Mapping[tuple[str, str], str]) -> PrincipalFor:
    """``principal_for`` from a ``(namespace, service_account) → principal`` table."""
    return lambda w: table.get((w.namespace, w.service_account))


OBJECT_RENDERERS: dict[str, Callable[..., Rendered]] = {
    "onprem": render_seaweedfs_identities,
    "aws": render_s3_bucket_policy,
    "gcp": render_gcs_iam_policy,
}


# --- secrets ----------------------------------------------------------------------------------------

def _consumers_in(binding: SecretBinding, namespace: str) -> list[Workload]:
    consumers = [c for c in binding.consumers if c.namespace == namespace]
    if not consumers:
        raise NeutralError("NO_CONSUMER_IN_NAMESPACE")
    return consumers


def render_openbao_delivery(binding: SecretBinding, *, delivery, namespace: str) -> Rendered:
    """onprem: the existing OpenBao delivery. Paths, role body and init container are T9's
    renderer output (``OpenBaoDelivery``), not re-implemented here."""
    consumers = _consumers_in(binding, namespace)
    return Rendered("onprem", {
        "logical_name": binding.logical_name, "keys": list(binding.keys),
        "service_accounts": [c.service_account for c in consumers],
        "remote_path": delivery.remote_path(namespace), "role_name": delivery.role_name(namespace),
        "auth_role": delivery.auth_role(namespace), "init_container": delivery.init_container(namespace)})


def _external_secret(binding: SecretBinding, namespace: str, store: str, remote_key: str) -> dict:
    return {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
            "metadata": {"name": binding.logical_name, "namespace": namespace},
            "spec": {"refreshInterval": "1h", "secretStoreRef": {"name": store, "kind": "SecretStore"},
                     "target": {"name": binding.logical_name, "creationPolicy": "Owner"},
                     "data": [{"secretKey": k, "remoteRef": {"key": remote_key, "property": k}}
                              for k in binding.keys]}}


def _store(namespace: str, name: str, provider: dict) -> dict:
    return {"apiVersion": "external-secrets.io/v1", "kind": "SecretStore",
            "metadata": {"name": name, "namespace": namespace}, "spec": {"provider": provider}}


def _check(pattern: str, *values: str) -> None:
    if any(not isinstance(v, str) or re.fullmatch(pattern, v) is None for v in values):
        raise NeutralError("INVALID_TARGET_CONTEXT")


def render_eso_aws(binding: SecretBinding, *, namespace: str, region: str, remote_key: str,
                   store_service_account: str, store: str = "target-secrets") -> Rendered:
    """aws: AWS Secrets Manager + ESO. The store authenticates with the IRSA token of
    ``store_service_account`` (not the app's); the app reads the resulting Kubernetes Secret."""
    _consumers_in(binding, namespace)
    _check(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]", region)
    _check(r"[A-Za-z0-9/_+=.@-]{1,512}", remote_key)
    _check(r"[a-z0-9]([-.a-z0-9]{0,251}[a-z0-9])?", store_service_account, store)
    provider = {"aws": {"service": "SecretsManager", "region": region,
                        "auth": {"jwt": {"serviceAccountRef": {"name": store_service_account}}}}}
    return Rendered("aws", [_store(namespace, store, provider),
                            _external_secret(binding, namespace, store, remote_key)])


def render_eso_gcp(binding: SecretBinding, *, namespace: str, project_id: str, remote_key: str,
                   store_service_account: str, cluster_location: str, cluster_name: str,
                   store: str = "target-secrets") -> Rendered:
    """gcp: GCP Secret Manager + ESO through GKE Workload Identity of ``store_service_account``.
    The secret payload is one JSON object; each key is a ``property``."""
    _consumers_in(binding, namespace)
    _check(r"[a-z][-a-z0-9]{4,28}[a-z0-9]", project_id)
    _check(r"[A-Za-z0-9_-]{1,255}", remote_key)
    _check(r"[a-z0-9]([-.a-z0-9]{0,251}[a-z0-9])?", store_service_account, store)
    _check(r"[a-z0-9-]{1,63}", cluster_location, cluster_name)
    provider = {"gcpsm": {"projectID": project_id, "auth": {"workloadIdentity": {
        "clusterLocation": cluster_location, "clusterName": cluster_name, "clusterProjectID": project_id,
        "serviceAccountRef": {"name": store_service_account}}}}}
    return Rendered("gcp", [_store(namespace, store, provider),
                            _external_secret(binding, namespace, store, remote_key)])


SECRET_RENDERERS: dict[str, Callable[..., Rendered]] = {
    "onprem": render_openbao_delivery,
    "aws": render_eso_aws,
    "gcp": render_eso_gcp,
}
