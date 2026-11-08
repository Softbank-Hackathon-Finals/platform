"""T44 destination build approval (ADR-0032): a human approves one infra plan digest on a migration plan.

The infra plan service (services/infra-plan) recomputes its plan digest, then claims the approval
once for one run. This module never touches a cluster or Git; it only records the human decision.

- Approval is bound to the migration plan whose target is being built, and only before SHIFT.
- The approver must not be the requester recorded in the binding (agent or human).
- A claim needs `apply_infra` now, and the approver must still hold `approve_infra` now.
- A claim carries the caller's whole expected binding; every field must equal the approved one.
- No approval.decided event: the T10 events v1 schema has no "infra" kind (same as shift).
  T44 records each run in the T24 ledger instead.
"""

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .contract import validate_identifier
from .cutover import _get, _put
from .plan_api import Principal, authorized_plan
from .plans import _canonical
from .store import EventError

INFRA_APPROVAL_STATES = frozenset({"COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE", "AWAIT_APPROVAL"})
MAX_INFRA_APPROVAL = timedelta(hours=24)
BINDING_KEYS = frozenset({"infra_plan_id", "plan_digest", "requested_by"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def infra_key(kind, value):
    return f"infra-{kind}-{value}"


def _binding(value):
    if not isinstance(value, dict) or set(value) != BINDING_KEYS:
        raise ValueError("invalid_infra_binding")
    validate_identifier(value["infra_plan_id"])
    validate_identifier(value["requested_by"])
    if not isinstance(value["plan_digest"], str) or not _DIGEST.match(value["plan_digest"]):
        raise ValueError("invalid_infra_binding")
    return dict(value)


class InfraApprovalService:
    def __init__(self, store, *, check_approve, check_apply, check_access, check_approver, clock=None):
        if not all(callable(f) for f in (check_approve, check_apply, check_access, check_approver)):
            raise ValueError("infra_approval_permission_required")
        self.store = store
        self.check_approve, self.check_apply = check_approve, check_apply
        self.check_access, self.check_approver = check_access, check_approver
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def approve(self, principal, plan_id, *, binding, expires_at, command_key):
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        binding = _binding(binding)
        if binding["requested_by"] == principal.subject_id:
            raise EventError("self_approval_forbidden")
        request = dict(binding=binding, expires_at=expires_at, actor_id=principal.subject_id)
        key = infra_key("approval-key", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_approve)
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1]
            if plan["state"] not in INFRA_APPROVAL_STATES:
                raise EventError("plan_state_conflict")
            expires = datetime.fromisoformat(expires_at)
            if not datetime.fromisoformat(now) < expires <= datetime.fromisoformat(now) + MAX_INFRA_APPROVAL:
                raise EventError("invalid_expiry")
            receipt = dict(approval_id=str(uuid4()), approval_kind="infra", binding=binding,
                           plan_version=plan["version"], actor_id=principal.subject_id, approved_at=now,
                           expires_at=expires_at)
            _put(c, plan_id, key, request, receipt)
            _put(c, plan_id, infra_key("approval", receipt["approval_id"]), request, receipt)
            return receipt

    def read(self, principal, plan_id, approval_id):
        """The receipt and whether it is still usable. Never reveals other plans' approvals."""
        validate_identifier(approval_id)
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_access)
            saved = _get(c, plan_id, infra_key("approval", approval_id))
            if saved is None:
                raise EventError("approval_unavailable")
            claim = _get(c, plan_id, infra_key("consumed", approval_id))
            revoked = _get(c, plan_id, infra_key("operator-revoked-approval", approval_id))
            return saved[1] | dict(revoked=revoked is not None, claimed_run_id=claim[1]["run_id"] if claim else None)

    def claim(self, principal, plan_id, approval_id, *, run_id, binding):
        """Single use: one approval starts one run. A replay of the same run returns the same claim.

        `binding` is the caller's whole expected binding `{infra_plan_id, plan_digest, requested_by}`;
        all three must equal the approved one. A digest match alone is not enough: the same digest
        may be requested twice (another plan id or requester) and approved for only one of them.
        """
        for value in (approval_id, run_id):
            validate_identifier(value)
        try:
            binding = _binding(binding)
        except ValueError:
            raise EventError("binding_mismatch") from None
        request = dict(approval_id=approval_id, run_id=run_id, binding=binding)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_apply)
            saved = _get(c, plan_id, infra_key("approval", approval_id))
            if saved is None:
                raise EventError("approval_unavailable")
            receipt = saved[1]
            old = _get(c, plan_id, infra_key("consumed", approval_id))
            if old:
                # Compare the stored claim, not the stored request: claims recorded before the whole
                # binding was required stored only the digest in the request.
                if old[1]["run_id"] != run_id or _canonical(old[1]["binding"]) != _canonical(binding):
                    raise EventError("approval_already_consumed")
                return old[1]
            if _get(c, plan_id, infra_key("operator-revoked-approval", approval_id)):
                raise EventError("forbidden")
            if _canonical(receipt["binding"]) != _canonical(binding):
                raise EventError("binding_mismatch")
            if datetime.fromisoformat(now) >= datetime.fromisoformat(receipt["expires_at"]):
                raise EventError("approval_expired")
            if plan["state"] not in INFRA_APPROVAL_STATES:
                raise EventError("plan_state_conflict")
            self.check_approver(c, plan, receipt["actor_id"])
            claimed = dict(approval_id=approval_id, run_id=run_id, binding=receipt["binding"],
                           approved_by=receipt["actor_id"], claimed_by=principal.subject_id, claimed_at=now)
            _put(c, plan_id, infra_key("consumed", approval_id), request, claimed)
            return claimed
