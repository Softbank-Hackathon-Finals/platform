"""Slot `source.neutral_export`: source object / secret policy → neutral model (ADR-0034 §3.2.4).

One exporter per source kind, registered by kind. Exporters are pure: they read the policy text
or secret *metadata* the collector already holds and never call a cloud API. Secret values are
refused, not stripped. A principal that cannot be mapped to a workload becomes ``unresolved``;
a statement that the neutral model cannot express without widening access is refused
(``NeutralError``) so the item becomes agent_needed instead of silently changing meaning.

``principals`` is the deploy trace's answer (slot `source.deploy_trace`): the source principal
string exactly as it appears in the policy → the workload that holds it. Two forms are mapped
without it because they name the workload themselves: the Kubernetes subject
``system:serviceaccount:<ns>:<sa>`` and GKE Workload Identity members.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping

from .models import (
    ACTIONS,
    Anonymous,
    Grant,
    NeutralError,
    ObjectAccessPolicy,
    Principal,
    SecretBinding,
    SourceRef,
    Unresolved,
    Workload,
    canonical_actions,
)

MAX_POLICY_BYTES = 32768
# Split literal: the public-evidence scan reads every "arn" + ":aws:" text as an ARN to mask.
S3_ARN = "arn:" + "aws:s3:::"
ALL = frozenset(ACTIONS)

K8S_SUBJECT = re.compile(r"^system:serviceaccount:([a-z0-9][-a-z0-9]{0,61}[a-z0-9]?):([a-z0-9][-.a-z0-9]{0,251}[a-z0-9]?)$")
GKE_WI_MEMBER = re.compile(r"^serviceAccount:[a-z][-a-z0-9]{4,28}[a-z0-9]\.svc\.id\.goog\[([^/\]]+)/([^\]]+)\]$")
GKE_WI_PRINCIPAL = re.compile(
    r"^principal://iam\.googleapis\.com/projects/[0-9]+/locations/global/workloadIdentityPools/"
    r"[a-z][-a-z0-9]{4,28}[a-z0-9]\.svc\.id\.goog/subject/ns/([^/]+)/sa/([^/]+)$")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise NeutralError("DUPLICATE_KEY")
        out[key] = value
    return out


def parse_json(text: str) -> object:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise NeutralError("POLICY_TOO_LARGE")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except NeutralError:
        raise
    except (ValueError, RecursionError):
        raise NeutralError("POLICY_UNPARSEABLE") from None


def mask(ref: str) -> str:
    """Hide account / project identifiers in a principal reference (public-evidence rule)."""
    ref = re.sub(r"(arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:)[0-9]{12}(:)", r"\1***\2", ref)
    ref = re.sub(r"@[a-z][-a-z0-9]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$", "@***.iam.gserviceaccount.com", ref)
    ref = re.sub(r"projects/[0-9]+/", "projects/***/", ref)
    ref = re.sub(r"^(project(?:Owner|Editor|Viewer)):.+$", r"\1:***", ref)
    return ref[:256]


def _workload(ns: str, sa: str) -> Workload | None:
    try:
        return Workload(ns, sa)
    except NeutralError:
        return None


def resolve_principal(ref: str, principals: Mapping[str, Workload]) -> Principal:
    mapped = principals.get(ref)
    if mapped is not None:
        if not isinstance(mapped, Workload):
            raise NeutralError("INVALID_PRINCIPAL_MAP")
        return mapped
    for pattern in (K8S_SUBJECT, GKE_WI_MEMBER, GKE_WI_PRINCIPAL):
        match = pattern.fullmatch(ref)
        if match and (workload := _workload(*match.groups())):
            return workload
    return Unresolved(mask(ref))


def _principal_key(principal: Principal) -> tuple:
    if isinstance(principal, Anonymous):
        return (0, "", "")
    if isinstance(principal, Workload):
        return (1, principal.namespace, principal.service_account)
    return (2, principal.ref, "")


class _Grants:
    """Union of actions per (principal, prefix), emitted in a canonical order."""

    def __init__(self):
        self.actions: dict[tuple[Principal, str], set[str]] = {}

    def add(self, principal: Principal, actions: Iterable[str], prefix: str = "") -> None:
        actions = set(actions)
        if actions:
            self.actions.setdefault((principal, prefix), set()).update(actions)

    def remove(self, match: Callable[[Principal], bool], actions: set[str]) -> None:
        for key in list(self.actions):
            if match(key[0]):
                self.actions[key] -= actions
                if not self.actions[key]:
                    del self.actions[key]

    def grants(self) -> tuple[Grant, ...]:
        keys = sorted(self.actions, key=lambda k: (_principal_key(k[0]), k[1]))
        if len(keys) > 64:
            raise NeutralError("TOO_MANY_GRANTS")
        return tuple(Grant(p, canonical_actions(self.actions[(p, prefix)]), prefix) for p, prefix in keys)


def _policy(bucket: str, source: SourceRef, grants: _Grants, tls_required: bool) -> ObjectAccessPolicy:
    built = grants.grants()
    anonymous = [g for g in built if isinstance(g.principal, Anonymous)]
    return ObjectAccessPolicy(bucket, source, built, any("read" in g.actions for g in anonymous),
                              any("list" in g.actions for g in anonymous), tls_required)


# --- aws: S3 bucket policy ------------------------------------------------------------------------

S3_ACTIONS = {
    "s3:GetObject": {"read"}, "s3:GetObjectVersion": {"read"}, "s3:Get*": {"read"},
    "s3:PutObject": {"write"}, "s3:Put*": {"write"},
    "s3:ListBucket": {"list"}, "s3:List*": {"list"},
    "s3:DeleteObject": {"delete"}, "s3:DeleteObjectVersion": {"delete"}, "s3:Delete*": {"delete"},
    "s3:*": set(ALL), "*": set(ALL),
}
S3_IGNORED = {"s3:GetBucketLocation"}  # metadata read with no object access
ANONYMOUS_CONDITIONS = (
    {"StringEquals": {"aws:PrincipalType": "Anonymous"}},
    {"StringEquals": {"aws:PrincipalType": ["Anonymous"]}},
)
TLS_CONDITIONS = ({"Bool": {"aws:SecureTransport": "false"}}, {"Bool": {"aws:SecureTransport": False}},
                  {"Bool": {"aws:SecureTransport": ["false"]}})


def _strings(value) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise NeutralError("POLICY_UNTRANSLATABLE")
    return values


def _s3_principals(value) -> list[str] | None:
    """``None`` means everyone (``"*"``); otherwise the AWS principal strings."""
    if value == "*":
        return None
    if not isinstance(value, dict) or not value or not set(value) <= {"AWS", "Service", "Federated", "CanonicalUser"}:
        raise NeutralError("POLICY_UNTRANSLATABLE")
    refs = []
    for key, entry in value.items():
        for ref in _strings(entry):
            if key == "AWS" and ref == "*":
                return None
            refs.append(ref if key == "AWS" else f"{key}:{ref}")
    return refs


def _s3_scope(resources: list[str], bucket: str) -> tuple[bool, set[str]] | None:
    """(bucket-level resource present, object prefixes) for ``bucket``; ``None`` if not this bucket."""
    bucket_level, prefixes, other = False, set(), True
    for resource in resources:
        if resource == f"{S3_ARN}{bucket}":
            bucket_level, other = True, False
        elif resource.startswith(f"{S3_ARN}{bucket}/"):
            other = False
            path = resource[len(f"{S3_ARN}{bucket}/"):]
            if not path.endswith("*") or "*" in path[:-1] or "?" in path:
                raise NeutralError("RESOURCE_UNTRANSLATABLE")
            prefixes.add(path[:-1])
        elif resource in ("*", S3_ARN + "*"):
            bucket_level, other = True, False
            prefixes.add("")
    return None if other else (bucket_level, prefixes)


def _s3_actions(values: list[str]) -> tuple[set[str], bool]:
    """(neutral actions, every action understood)."""
    out, known = set(), True
    for action in values:
        if action in S3_ACTIONS:
            out |= S3_ACTIONS[action]
        elif action not in S3_IGNORED:
            known = False
    return out, known


def _only(names: set[str]) -> str:
    """The single bucket a policy names, when the caller did not say which one."""
    if len(names) != 1:
        raise NeutralError("BUCKET_AMBIGUOUS")
    return names.pop()


def export_s3_bucket_policy(text: str, *, bucket: str | None = None,
                            principals: Mapping[str, Workload] | None = None) -> ObjectAccessPolicy:
    principals = principals or {}
    doc = parse_json(text)
    if not isinstance(doc, dict) or not set(doc) <= {"Version", "Id", "Statement"} or "Statement" not in doc:
        raise NeutralError("POLICY_UNTRANSLATABLE")
    statements = doc["Statement"]
    statements = [statements] if isinstance(statements, dict) else statements
    if not isinstance(statements, list):
        raise NeutralError("POLICY_UNTRANSLATABLE")
    if bucket is None:
        bucket = _only({r[len(S3_ARN):].split("/")[0] for st in statements if isinstance(st, dict)
                        for r in _strings(st.get("Resource", "*")) if r.startswith(S3_ARN)} - {"*"})
    grants, tls_required, denies = _Grants(), False, []
    for index, st in enumerate(statements):
        if (not isinstance(st, dict) or not {"Effect", "Principal", "Action", "Resource"} <= set(st)
                or not set(st) <= {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}):
            raise NeutralError("POLICY_UNTRANSLATABLE")  # NotPrincipal/NotAction/NotResource included
        scope = _s3_scope(_strings(st["Resource"]), bucket)
        if scope is None:
            continue  # another bucket's statement
        bucket_level, prefixes = scope
        actions, known = _s3_actions(_strings(st["Action"]))
        refs = _s3_principals(st["Principal"])
        condition = st.get("Condition")
        if st["Effect"] == "Deny":
            if condition in TLS_CONDITIONS and refs is None:
                tls_required = True
                continue
            denies.append((refs, actions, condition, known))
            continue
        if st["Effect"] != "Allow":
            raise NeutralError("POLICY_UNTRANSLATABLE")
        object_actions = actions - {"list"}
        listing = "list" in actions and bucket_level
        if not listing and not (object_actions and prefixes):
            continue
        targets: list[Principal] = [Anonymous()] if refs is None else [resolve_principal(r, principals) for r in refs]
        sid = st.get("Sid") if isinstance(st.get("Sid"), str) else f"statement-{index}"
        for principal in targets:
            if condition is not None or not known:
                # A conditional or partly understood grant cannot be rendered with the same meaning.
                ref = principal.ref if isinstance(principal, Unresolved) else _describe(principal)
                principal = Unresolved(mask(f"{ref}#{'condition' if condition is not None else 'action'}:{sid}"))
            if listing:
                grants.add(principal, {"list"})
            for prefix in sorted(prefixes):
                grants.add(principal, object_actions, prefix)
    for refs, actions, condition, known in denies:
        if not known:
            raise NeutralError("DENY_UNREPRESENTABLE")
        if refs is None and condition in ANONYMOUS_CONDITIONS:
            grants.remove(lambda p: isinstance(p, Anonymous), actions)
        elif refs is not None and condition is None and all(
                isinstance(resolve_principal(r, principals), Workload) for r in refs):
            denied = {resolve_principal(r, principals) for r in refs}
            grants.remove(lambda p, denied=denied: p in denied, actions)
        else:
            # e.g. a Deny to everyone: identities outside the policy keep their access on a target
            # whose bucket policy is additive, so dropping it would widen access.
            raise NeutralError("DENY_UNREPRESENTABLE")
    return _policy(bucket, SourceRef("aws", bucket), grants, tls_required)


def _describe(principal: Principal) -> str:
    if isinstance(principal, Workload):
        return f"system:serviceaccount:{principal.namespace}:{principal.service_account}"
    return "anonymous"


# --- gcp: GCS bucket IAM policy -------------------------------------------------------------------

GCS_ROLES = {
    "roles/storage.objectViewer": {"read", "list"},
    "roles/storage.objectCreator": {"write"},
    "roles/storage.objectUser": set(ALL),
    "roles/storage.objectAdmin": set(ALL),
    "roles/storage.admin": set(ALL),
    "roles/storage.legacyObjectReader": {"read"},
    "roles/storage.legacyObjectOwner": {"read"},
    "roles/storage.legacyBucketReader": {"list"},
    "roles/storage.legacyBucketWriter": {"list", "write", "delete"},
    "roles/storage.legacyBucketOwner": {"list", "write", "delete"},
}


def export_gcs_iam_policy(text: str, *, bucket: str | None = None, principals: Mapping[str, Workload] | None = None,
                          tls_required: bool = True) -> ObjectAccessPolicy:
    """GCS IAM cannot state a TLS requirement; ``tls_required`` defaults to the stricter value.
    Without ``bucket`` the name comes from ``resourceId`` (``projects/_/buckets/<name>``)."""
    principals = principals or {}
    doc = parse_json(text)
    if (not isinstance(doc, dict) or not set(doc) <= {"version", "etag", "bindings", "kind", "resourceId"}
            or not isinstance(doc.get("bindings", []), list)):
        raise NeutralError("POLICY_UNTRANSLATABLE")
    if bucket is None:
        match = re.fullmatch(r"projects/_/buckets/([^/]+)", str(doc.get("resourceId", "")))
        if match is None:
            raise NeutralError("BUCKET_AMBIGUOUS")
        bucket = match.group(1)
    grants = _Grants()
    for binding in doc.get("bindings", []):
        if not isinstance(binding, dict) or not {"role", "members"} <= set(binding) <= {"role", "members", "condition"}:
            raise NeutralError("POLICY_UNTRANSLATABLE")
        actions = GCS_ROLES.get(binding["role"])
        if actions is None:
            raise NeutralError("ROLE_UNTRANSLATABLE")
        for member in _strings(binding["members"]):
            if member in ("allUsers", "allAuthenticatedUsers"):
                principal: Principal = Anonymous()  # any Google account is the public
            else:
                principal = resolve_principal(member, principals)
            if "condition" in binding:
                ref = principal.ref if isinstance(principal, Unresolved) else _describe(principal)
                principal = Unresolved(mask(f"{ref}#condition"))
            grants.add(principal, actions)
    return _policy(bucket, SourceRef("gcp", bucket), grants, tls_required)


# --- onprem: SeaweedFS s3.json identities ---------------------------------------------------------

SEAWEED_ACTIONS = {"Read": {"read"}, "Write": {"write", "delete"}, "List": {"list"}, "Admin": set(ALL),
                   "Tagging": set()}


def export_seaweedfs_identities(text: str, *, bucket: str | None = None,
                                principals: Mapping[str, Workload] | None = None,
                                tls_required: bool = True) -> ObjectAccessPolicy:
    """Identity names are the principals; ``anonymous`` is the public. Credentials are never read
    into the model. SeaweedFS cannot state a TLS requirement; the stricter default is used."""
    principals = principals or {}
    doc = parse_json(text)
    if not isinstance(doc, dict) or not set(doc) <= {"identities"} or not isinstance(doc.get("identities"), list):
        raise NeutralError("POLICY_UNTRANSLATABLE")
    if not all(isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("actions", []), list)
               for i in doc["identities"]):
        raise NeutralError("POLICY_UNTRANSLATABLE")
    if bucket is None:
        bucket = _only({a.partition(":")[2].split("/")[0] for i in doc["identities"] for a in i.get("actions", [])
                        if isinstance(a, str) and a.partition(":")[2]})
    grants = _Grants()
    for identity in doc["identities"]:
        name = identity["name"]
        principal = Anonymous() if name == "anonymous" else resolve_principal(name, principals)
        for entry in identity.get("actions", []):
            if not isinstance(entry, str):
                raise NeutralError("POLICY_UNTRANSLATABLE")
            action, _, target = entry.partition(":")
            if action not in SEAWEED_ACTIONS:
                raise NeutralError("ACTION_UNTRANSLATABLE")
            if target and target != bucket and not target.startswith(bucket + "/"):
                continue
            prefix = target[len(bucket) + 1:] if target.startswith(bucket + "/") else ""
            grants.add(principal, SEAWEED_ACTIONS[action], prefix.rstrip("*"))
    return _policy(bucket, SourceRef("onprem", bucket), grants, tls_required)


OBJECT_EXPORTERS: dict[str, Callable[..., ObjectAccessPolicy]] = {
    "aws": export_s3_bucket_policy,
    "gcp": export_gcs_iam_policy,
    "onprem": export_seaweedfs_identities,
}


# --- secrets: metadata only -----------------------------------------------------------------------

VALUE_FIELDS = {"SecretString", "SecretBinary", "payload", "data", "secret_data", "value"}


def logical_name(name: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:63].strip("-")
    if not value:
        raise NeutralError("INVALID_LOGICAL_NAME")
    return value


def _consumers(refs: Iterable[str], principals: Mapping[str, Workload]) -> tuple[tuple[Workload, ...], tuple[str, ...]]:
    mapped, unresolved = [], []
    for ref in refs:
        principal = resolve_principal(ref, principals)
        if isinstance(principal, Workload):
            if principal not in mapped:
                mapped.append(principal)
        else:
            unresolved.append(principal.ref)
    if not mapped:
        raise NeutralError("CONSUMER_UNRESOLVED")
    return tuple(sorted(mapped, key=_principal_key)), tuple(unresolved)


def _secret(kind: str, name: str, metadata: dict, keys, consumers, principals, rotation: bool) -> SecretBinding:
    if not isinstance(metadata, dict) or VALUE_FIELDS & set(metadata):
        raise NeutralError("SECRET_VALUE_PRESENT")
    mapped, unresolved = _consumers(consumers, principals or {})
    return SecretBinding(logical_name(name), SourceRef(kind, name), tuple(keys), mapped, rotation, unresolved)


def export_aws_secret(metadata: dict, *, keys, consumers, principals=None) -> SecretBinding:
    """``metadata`` is ``secretsmanager describe-secret`` output; the ARN is not kept."""
    name = metadata.get("Name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name:
        raise NeutralError("SECRET_NAME_MISSING")
    return _secret("aws", name, metadata, keys, consumers, principals, metadata.get("RotationEnabled") is True)


def export_gcp_secret(metadata: dict, *, keys, consumers, principals=None) -> SecretBinding:
    """``metadata`` is a Secret Manager ``Secret`` resource (``projects/<p>/secrets/<name>``)."""
    full = metadata.get("name") if isinstance(metadata, dict) else None
    match = re.fullmatch(r"projects/[^/]+/secrets/([A-Za-z0-9_-]{1,255})", full or "")
    if match is None:
        raise NeutralError("SECRET_NAME_MISSING")
    return _secret("gcp", match.group(1), metadata, keys, consumers, principals, "rotation" in metadata)


def export_openbao_secret(metadata: dict, *, keys, consumers, principals=None) -> SecretBinding:
    """``metadata`` names a KV v2 path (``{"mount", "path", ...}`` from ``bao kv metadata get``)."""
    path = metadata.get("path") if isinstance(metadata, dict) else None
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", path) or ".." in path:
        raise NeutralError("SECRET_NAME_MISSING")
    custom = metadata.get("custom_metadata") or {}
    return _secret("onprem", path, metadata, keys, consumers, principals,
                   isinstance(custom, dict) and custom.get("rotation_required") == "true")


SECRET_EXPORTERS: dict[str, Callable[..., SecretBinding]] = {
    "aws": export_aws_secret,
    "gcp": export_gcp_secret,
    "onprem": export_openbao_secret,
}
