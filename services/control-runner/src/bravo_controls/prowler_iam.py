"""Qualify Prowler's AWS portion of IAM-01.2 against independently read policies.

This does NOT produce an IAM-01.2 PASS: Kubernetes RBAC and the approved baseline
mapping must also be qualified. The first supported scanner shape is Prowler
5.47.0, one inline policy, no attached managed policy or permissions boundary.
The trusted worker owns clients, scanner invocation, diagnostics and mapping;
none of these inputs may be accepted from an uploaded report or browser.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from .aws_identity import RegisteredRole
from .tool_reports import (
    CheckMapping,
    ReportAssessment,
    ToolRun,
    _InvalidReport,
    _load,
    _mapping,
    assess_prowler,
)

VERSION = "5.47.0"
CHECKS = (
    "iam_inline_policy_no_administrative_privileges",
    "iam_inline_policy_allows_privilege_escalation",
)
MAX_POLICIES = 20
MAX_PAGES = 5
MAX_DOCUMENT_BYTES = 256 * 1024
_NAME = re.compile(r"[A-Za-z0-9+=,.@_-]{1,128}\Z")


class InventoryUnavailable(ValueError):
    """Only fixed error codes; provider messages and policy values stay private."""


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise InventoryUnavailable("invalid_policy_document") from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise InventoryUnavailable("policy_document_too_large")
    return raw


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _document(value):
    # No interpretation/rewrite of IAM semantics here. Empty/missing documents
    # cannot pass a scanner whose `if policy.document` would skip the analysis.
    if type(value) is not dict or value.get("Version") != "2012-10-17":
        raise InventoryUnavailable("policy_document_unconfirmed")
    statements = value.get("Statement")
    if not (
        isinstance(statements, dict)
        and statements
        or isinstance(statements, list)
        and statements
        and all(isinstance(s, dict) and s for s in statements)
    ):
        raise InventoryUnavailable("policy_document_unconfirmed")
    return _canonical(value)


@dataclass(frozen=True, repr=False)
class RoleInventory:
    role: RegisteredRole
    trust: bytes = field(repr=False)
    inline: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def digest(self):
        return _sha(
            _canonical(
                {
                    "role": self.role.arn,
                    "role_id": self.role.role_id,
                    "trust": self.trust.decode(),
                    "inline": [(name, raw.decode()) for name, raw in self.inline],
                }
            )
        )


def read_role_inventory(client, role: RegisteredRole) -> RoleInventory:
    """Bounded Get/List only, using a host-authenticated read-only IAM client.

    The host pins the SDK endpoint, disables wire debug, enforces the overall job
    deadline and records collection times. Role identity is the subject being read,
    not the operator's identity. An unsupported shape raises, never becomes empty.
    """
    if not isinstance(role, RegisteredRole):
        raise InventoryUnavailable("registered_role_required")
    name = role.arn.rsplit("/", 1)[1]

    def call(operation, **args):
        try:
            response = getattr(client, operation)(**args)
        except Exception:
            raise InventoryUnavailable("iam_read_failed") from None
        if (
            type(response) is not dict
            or type(response.get("ResponseMetadata")) is not dict
            or response["ResponseMetadata"].get("HTTPStatusCode") != 200
        ):
            raise InventoryUnavailable("iam_read_unconfirmed")
        return response

    def listing(operation, key):
        marker, markers, rows = None, set(), []
        for _ in range(MAX_PAGES):
            page = call(
                operation, RoleName=name, MaxItems=20, **({"Marker": marker} if marker else {})
            )
            values = page.get(key)
            if (
                type(values) is not list
                or len(values) > 20
                or type(page.get("IsTruncated")) is not bool
            ):
                raise InventoryUnavailable("iam_listing_unconfirmed")
            rows.extend(values)
            if len(rows) > MAX_POLICIES:
                raise InventoryUnavailable("iam_inventory_budget_exceeded")
            if not page["IsTruncated"]:
                return rows
            marker = page.get("Marker")
            if not isinstance(marker, str) or not 1 <= len(marker) <= 1024 or marker in markers:
                raise InventoryUnavailable("iam_listing_unconfirmed")
            markers.add(marker)
        raise InventoryUnavailable("iam_inventory_budget_exceeded")

    def identity():
        value = call("get_role", RoleName=name).get("Role")
        if (
            type(value) is not dict
            or value.get("Arn") != role.arn
            or value.get("RoleId") != role.role_id
            or value.get("RoleName") != name
        ):
            raise InventoryUnavailable("iam_role_identity_changed")
        if "PermissionsBoundary" in value:
            raise InventoryUnavailable("managed_or_boundary_policy_unqualified")
        return _document(value.get("AssumeRolePolicyDocument"))

    trust = identity()
    names = listing("list_role_policies", "PolicyNames")
    if (
        not names
        or any(not isinstance(n, str) or not _NAME.fullmatch(n) for n in names)
        or len(names) != len(set(names))
    ):
        raise InventoryUnavailable("inline_policy_set_unconfirmed")
    if listing("list_attached_role_policies", "AttachedPolicies"):
        raise InventoryUnavailable("managed_or_boundary_policy_unqualified")
    policies = []
    for policy_name in sorted(names):
        row = call("get_role_policy", RoleName=name, PolicyName=policy_name)
        if row.get("RoleName") != name or row.get("PolicyName") != policy_name:
            raise InventoryUnavailable("inline_policy_binding_changed")
        policies.append((policy_name, _document(row.get("PolicyDocument"))))
    if identity() != trust:
        raise InventoryUnavailable("iam_role_identity_changed")
    return RoleInventory(role, trust, tuple(policies))


def assess_iam_collection(
    run: ToolRun,
    mapping: CheckMapping | None,
    *,
    before: RoleInventory,
    after: RoleInventory,
    baseline_digest: str,
    diagnostics: bytes,
) -> ReportAssessment:
    """Check execution diagnostics AND independently read policy provenance.

    `diagnostics` contains all SDK stdout/stderr and WARNING+ log records. Prowler
    can emit PASS after a caught analysis error; exit code zero is insufficient.
    Do not filter error lines or mute findings. The approved baseline digest comes
    from server configuration, never from the report or freshly observed inventory.
    """

    def unknown(reason, detail):
        raw = run.output if isinstance(run, ToolRun) and isinstance(run.output, bytes) else b""
        return ReportAssessment(None, reason, {"detail": detail}, _sha(raw))

    if (
        type(diagnostics) is not bytes
        or diagnostics
        or not isinstance(run, ToolRun)
        or run.complete is not True
    ):
        return unknown("TOOL_ERROR", "scanner_diagnostics_or_incomplete_collection")
    if (
        not isinstance(before, RoleInventory)
        or not isinstance(after, RoleInventory)
        or before != after
    ):
        return unknown("UNEXPECTED_RESPONSE", "policy_inventory_changed")
    if (
        len(before.inline) != 1
        or not _mapping(mapping, "prowler")
        or mapping.version != VERSION
        or run.version != VERSION
        or run.target != before.role.arn
        or set(mapping.required) != {(c, before.role.arn) for c in CHECKS}
    ):
        return unknown("NOT_RUN", "iam_mapping_unqualified")
    if baseline_digest != before.digest:
        # A different policy may be narrower; do not falsely call it broader.
        # It requires a new reviewed baseline rather than an inferred PASS.
        return unknown("UNEXPECTED_RESPONSE", "approved_policy_baseline_changed")
    policy_name, policy_raw = before.inline[0]
    try:
        rows = _load(run.output)
        if type(rows) is not list or len(rows) != len(CHECKS):
            raise _InvalidReport()
        for row in rows:
            resources = row["resources"]
            if type(resources) is not list or len(resources) != 1:
                raise _InvalidReport()
            resource = resources[0]
            metadata = resource["data"]["metadata"]
            if (
                resource["uid"] != before.role.arn
                or metadata["arn"] != before.role.arn
                or metadata["name"] != policy_name
                or metadata["type"] != "Inline"
                or metadata["attached"] is not True
                or metadata["entity"] != before.role.arn.rsplit("/", 1)[1]
                or _document(metadata.get("document")) != policy_raw
            ):
                raise _InvalidReport()
    except (KeyError, TypeError, IndexError, _InvalidReport, InventoryUnavailable):
        return unknown("TOOL_ERROR", "scanner_policy_inventory_mismatch")
    return assess_prowler(run, mapping)
