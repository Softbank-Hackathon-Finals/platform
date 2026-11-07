"""Plan-scoped operator permission and revocation journal in the existing DB.

No permission-management HTTP endpoint is exposed. Changes enter through a
trusted host administration path with an authenticated administrator receipt.
No new database role, migration, global lock or independent policy store.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

from .change import ChangeApprovalService, change_key
from .change_intake_api import intake_router
from .contract import validate_identifier
from .cutover import CutoverService, _get, _key, _put, unavailable_authority
from .cutover_api import create_cutover_app
from .executor import _timestamp
from .failback import FailbackService, failback_key
from .infra_approval import InfraApprovalService, infra_key
from .operator_identity import OperatorPrincipal
from .plans import _canonical
from .rehearsal import RehearsalService
from .store import EventError

PERMISSIONS = frozenset({"read", "approve_cutover", "execute_cutover", "approve_change", "apply_change",
                         "approve_failback", "execute_failback", "start_rehearsal", "hold_rehearsal",
                         "approve_shift", "execute_shift", "approve_infra", "apply_infra"})
# T19 gate actions (gate_services): recording, binding and evaluating evidence, consuming a report for
# SHIFT/cutover, and publishing or managing signature records. Granted per plan like the others.
GATE_PERMISSIONS = frozenset({"gate:record", "gate:read", "gate:bind", "gate:evaluate", "gate:consume",
                              "gate:use-exception", "gate:publish-provenance", "gate:manage-provenance"})
PERMISSIONS = PERMISSIONS | GATE_PERMISSIONS
APPROVAL_KEYS = {"cutover": _key, "change": change_key, "failback": failback_key, "infra": infra_key}


class OperatorAccess:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    @staticmethod
    def _grant(c, plan_id, subject_id):
        prefix = _key("operator-grant", subject_id) + "-"
        row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                        "ORDER BY command_key DESC LIMIT 1", (plan_id, prefix + "%")).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def set_permissions(self, plan_id, principal: OperatorPrincipal, *, permissions,
                        valid_after: str, expires_at: str, expected_revision: int,
                        administration_ref: str, command_key: str):
        """Trusted host only. No self-grant or token-to-permission HTTP path.

        administration_ref identifies the host's actual authorized management
        decision; a string alone does not authenticate an administrator. The host
        must verify that decision before calling, and may not call from UI input.
        Revocation is an empty permission set. Expected revision protects stale
        administrative writers; every change shares the approval's plan lock.
        """
        if not isinstance(principal, OperatorPrincipal):
            raise EventError("forbidden")
        for value in (plan_id, principal.subject_id, principal.tenant_id, administration_ref, command_key):
            validate_identifier(value)
        permissions = sorted(set(permissions))
        if set(permissions) - PERMISSIONS or (permissions and "read" not in permissions):
            raise ValueError("invalid_operator_permissions")
        if type(expected_revision) is not int or not 0 <= expected_revision < 10**12:
            raise ValueError("invalid_operator_revision")
        if _timestamp(expires_at) <= _timestamp(valid_after):
            raise ValueError("invalid_operator_grant_lifetime")
        request = dict(subject_id=principal.subject_id, tenant_id=principal.tenant_id,
            permissions=permissions, valid_after=valid_after, expires_at=expires_at,
            expected_revision=expected_revision, administration_ref=administration_ref)
        key = _key("operator-admin", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = c.execute("SELECT tenant_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None or plan["tenant_id"] != principal.tenant_id:
                raise EventError("forbidden")
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1]
            grant = self._grant(c, plan_id, principal.subject_id)
            if (grant["revision"] if grant else 0) != expected_revision:
                raise EventError("version_conflict")
            receipt = request | {"revision": expected_revision + 1, "recorded_at": self.clock()}
            _put(c, plan_id, _key("operator-grant", principal.subject_id) + f"-{receipt['revision']:013}", request, receipt)
            _put(c, plan_id, key, request, receipt)
            return receipt

    def revoke_approval(self, plan_id, approval_id, *, administration_ref: str,
                        approval_kind="cutover"):
        """Trusted host revocation; durable, idempotent, never cancels remote work."""
        if approval_kind not in APPROVAL_KEYS:
            raise ValueError("invalid_approval_kind")
        key_for = APPROVAL_KEYS[approval_kind]
        for value in (plan_id, approval_id, administration_ref):
            validate_identifier(value)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            if _get(c, plan_id, key_for("approval", approval_id)) is None:
                raise EventError("approval_unavailable")
            # Once dispatch is durable, revocation cannot pretend to roll it back.
            if _get(c, plan_id, key_for("consumed", approval_id)):
                raise EventError("approval_already_consumed")
            key = key_for("operator-revoked-approval", approval_id)
            old = _get(c, plan_id, key)
            if old:
                return old[1]
            receipt = dict(approval_id=approval_id, administration_ref=administration_ref,
                           revoked_at=self.clock())
            _put(c, plan_id, key, {}, receipt)
            return receipt

    def require(self, c, principal, plan, permission):
        """Called AFTER the plan lock is acquired, including token expiry checks."""
        now = _timestamp(self.clock()).timestamp()
        if (not isinstance(principal, OperatorPrincipal) or principal.tenant_id != plan["tenant_id"]
                or not principal.issued_at <= now < principal.expires_at):
            raise EventError("forbidden")
        return self._require_subject(c, plan, principal.subject_id, permission, now, principal.authenticated_at)

    def _require_subject(self, c, plan, subject_id, permission, now, authenticated_at=None):
        grant = self._grant(c, plan["plan_id"], subject_id)
        if (grant is None or grant["tenant_id"] != plan["tenant_id"]
                or permission not in grant["permissions"]
                or not _timestamp(grant["valid_after"]).timestamp() <= now < _timestamp(grant["expires_at"]).timestamp()
                or (authenticated_at is not None and authenticated_at < _timestamp(grant["valid_after"]).timestamp())):
            raise EventError("forbidden")
        return grant

    def check_read(self, c, principal, plan):
        self.require(c, principal, plan, "read")

    def resolver(self, verify_identity):
        def resolve(request):
            principal = verify_identity(request)
            if not isinstance(principal, OperatorPrincipal):
                return None
            try:
                plan_id = request.path_params["plan_id"]
                validate_identifier(plan_id)
                with self.store._transaction() as c:
                    plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
                    if plan is None:
                        return None
                    self.check_read(c, principal, dict(plan))
                return principal
            except (EventError, ValueError, KeyError):
                return None
        return resolve

    def authority(self, upstream=unavailable_authority):
        """Compose current human permission with the real T19/SHIFT authority.

        Upstream remains required, runs in the SAME transaction, and must check
        full gate provenance/context/exception revocation, actual SHIFT/latency,
        security holds and command settings. This wrapper cannot manufacture it.
        """
        return self._authority(upstream, "approve_cutover", "execute_cutover", _key)

    def change_authority(self, upstream=unavailable_authority):
        """Same operator rules, with distinct S2 permissions and revocations."""
        return self._authority(upstream, "approve_change", "apply_change", change_key)

    def failback_authority(self, upstream=unavailable_authority):
        """Same operator rules for T20 failback: distinct permissions and its own revocations."""
        return self._authority(upstream, "approve_failback", "execute_failback", failback_key)

    def requires(self, permission):
        """A check for host services that act outside an approval (e.g. FAILBACK progress)."""
        def check(c, principal, plan):
            self.require(c, principal, plan, permission)
        return check

    def requires_subject(self, permission):
        """A check that a recorded actor (e.g. the original approver) still holds a permission now."""
        def check(c, plan, subject_id):
            self._require_subject(c, plan, subject_id, permission, _timestamp(self.clock()).timestamp())
        return check

    def _authority(self, upstream, approve_permission, apply_permission, key_for):
        def current(c, principal, plan, *, now, approved_by, approval_id, **selection):
            permission = apply_permission if approval_id else approve_permission
            self.require(c, principal, plan, permission)
            actor = approved_by or principal.subject_id
            grant = self._require_subject(c, plan, actor, approve_permission, _timestamp(self.clock()).timestamp())
            if approval_id and _get(c, plan["plan_id"], key_for("operator-revoked-approval", approval_id)):
                raise EventError("forbidden")
            context = upstream(c, principal, plan, now=now, approved_by=approved_by, approval_id=approval_id, **selection)
            # Time may pass while upstream verifies authoritative records.
            self.require(c, principal, plan, permission)
            self._require_subject(c, plan, actor, approve_permission, _timestamp(self.clock()).timestamp())
            revision = hashlib.sha256(_canonical(dict(upstream=context.revision,
                approved_by=actor, permission_revision=grant["revision"])).encode()).hexdigest()
            return replace(context, revision=revision)
        return current


def create_operator_cutover_app(store, executor, *, verifier, bindings=None, registered_pair=None,
                               authority=unavailable_authority,
                               change_authority=None, failback_authority=None,
                               read_integrity=unavailable_authority, edge=None, clock=None, rehearsal_host=None,
                               change_intake=None):
    """Explicit host wiring. No IdP, grants or gate/SHIFT fixture is installed."""
    if (registered_pair is None) == (bindings is None):
        raise ValueError("exactly_one_binding_source_required")
    if change_intake is not None:
        if change_intake.store is not store or change_authority is not None:
            raise ValueError("single_change_intake_required")
        change_authority = change_intake.current
    if registered_pair is not None:
        bindings = executor.verified_bindings(registered_pair)
    access = OperatorAccess(store, clock=clock)
    service = CutoverService(store, executor, bindings=bindings, authority=access.authority(authority),
                             check_access=access.check_read, clock=clock)
    change_service = None if change_authority is None else ChangeApprovalService(store,
        authority=access.change_authority(change_authority), check_access=access.check_read, clock=clock)
    failback_service = None if failback_authority is None else FailbackService(store, executor,
        bindings=bindings, authority=access.failback_authority(failback_authority), read_integrity=read_integrity,
        edge=edge, check_access=access.check_read, check_execute=access.requires("execute_failback"), clock=clock)
    rehearsals = None if rehearsal_host is None else RehearsalService(
        store, host=rehearsal_host, check_start=access.requires("start_rehearsal"))
    from .workflow_stages import ShiftApprovalService

    shift_service = ShiftApprovalService(store, check_approve=access.requires("approve_shift"), clock=clock)
    infra_service = InfraApprovalService(store, check_approve=access.requires("approve_infra"),
                                         check_apply=access.requires("apply_infra"), check_access=access.check_read,
                                         check_approver=access.requires_subject("approve_infra"), clock=clock)
    app = create_cutover_app(service, resolve_principal=access.resolver(verifier), rehearsal_service=rehearsals,
                             change_service=change_service, failback_service=failback_service,
                             shift_service=shift_service, infra_service=infra_service)
    if change_intake is not None:
        app.include_router(intake_router(change_intake))
    return app
