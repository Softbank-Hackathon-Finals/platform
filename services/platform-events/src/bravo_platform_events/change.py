"""S2 approval and single application claim in the existing plan transaction.

T15 verifies rehearsals; T31 applies changes. This module does neither. The host
must read the current verified T15 record under the same lock as revocations.
An application claim is durable admission, not a deployment result.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from .contract import validate_event, validate_identifier
from .cutover import _get, _key, _put, unavailable_authority
from .executor import _timestamp
from .plan_api import Principal, authorized_plan
from .plans import _canonical, change_key
from .rehearsal import require_current_run
from .store import EventError


@dataclass(frozen=True)
class CurrentChange:
    """Server-verified current candidate, never deserialized from an HTTP proof.

    The supplier checks T15 verification provenance/result, current artifact and
    target identity, confirmed cleanup, revocations and holds. Its revision covers
    those records. T15's RECEIPT_UNVERIFIED is not an eligible candidate.
    Missing, failed or uncertain verification must raise, not return a candidate.
    """

    binding: dict
    rehearsal_id: str
    verification_id: str
    revision: str


class ChangeAuthority(Protocol):
    def __call__(self, connection, principal: Principal, plan: dict, *,
                 rehearsal_id: str, now: str, approved_by: str | None, approval_id: str | None) -> CurrentChange: ...


def intake_binding(record):
    candidate = record["candidate"]
    return {field: candidate[field] for field in ("plan_id", "target_environment_id", "diff_hash", "image_digest")} | {
        "plan_digest": record["plan_digest"], "expires_at": record["expires_at"],
    }


class ChangeApprovalService:
    def __init__(self, store, *, authority: ChangeAuthority = unavailable_authority,
                 check_access=None, clock=None):
        self.store, self.authority, self.check_access = store, authority, check_access
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _current(self, c, principal, plan, now, *, rehearsal_id, approved_by=None, approval_id=None):
        if plan["state"] != "REHEARSE":
            raise EventError("transition_not_allowed")
        try:
            current = self.authority(c, principal, plan, rehearsal_id=rehearsal_id, now=now,
                                     approved_by=approved_by, approval_id=approval_id)
            if not isinstance(current, CurrentChange):
                raise ValueError("invalid_change_authority")
            current = CurrentChange(**json.loads(_canonical(asdict(current))))
            for value in (current.rehearsal_id, current.verification_id, current.revision):
                validate_identifier(value)
            validate_event(dict(schema_version="1.0", event_id=f"{plan['plan_id']}:1",
                sequence=1, plan_id=plan["plan_id"], run_id=current.rehearsal_id,
                occurred_at=now, event_type="approval.requested", data=dict(
                    approval_id="validation", approval_kind="change", binding=current.binding)))
            expires_at = _timestamp(current.binding["expires_at"])
        except EventError as error:
            if error.code == "forbidden":
                raise
            raise EventError("authority_unavailable") from None
        except Exception:
            raise EventError("authority_unavailable") from None
        if current.rehearsal_id != rehearsal_id:
            raise EventError("binding_mismatch")
        spec = json.loads(plan["spec_json"])
        if any(current.binding[k] != spec[k] for k in
               ("plan_id", "plan_digest", "target_environment_id")):
            raise EventError("binding_mismatch")
        managed = require_current_run(c, plan, rehearsal_id, current.verification_id)
        if managed is not None and current.binding != intake_binding(managed):
            raise EventError("binding_mismatch")
        if expires_at <= _timestamp(self.clock()):
            raise EventError("approval_expired")
        return current

    def approve(self, principal, plan_id, *, run_id, binding, expected_version, command_key):
        validate_identifier(run_id)
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(approval_kind="change", run_id=run_id, binding=binding, expected_version=expected_version,
                       actor_id=principal.subject_id)
        # Both kinds share the HTTP endpoint's idempotency namespace. Historical
        # cutover records remain compatible; changing kind is a different input.
        key = _key("approval-key", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_access)
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1]
            if type(expected_version) is not int or expected_version != plan["version"]:
                raise EventError("version_conflict")
            current = self._current(c, principal, plan, now, rehearsal_id=run_id)
            if _canonical(binding) != _canonical(current.binding):
                raise EventError("binding_mismatch")
            approval_id = str(uuid4())
            receipt = dict(approval_id=approval_id, approval_kind="change", run_id=run_id, binding=current.binding,
                actor_id=principal.subject_id, approved_at=now, plan_version=plan["version"],
                context=asdict(current))
            _put(c, plan_id, change_key("approval", approval_id), request, receipt)
            _put(c, plan_id, key, request, receipt)
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=current.rehearsal_id,
                event_type="approval.decided", occurred_at=now,
                producer_key=change_key("approved", approval_id), data=dict(
                    approval_id=approval_id, approval_kind="change", binding=current.binding,
                    actor_id=principal.subject_id, decision="approved"))
            return receipt

    def claim_application(self, principal, plan_id, *, approval_id, command_key):
        """Host-only T31 admission port; returns (saved claim, newly_created).

        Only the first committed claim may initiate an effect. A retry/restart
        returns newly_created=False and must reconcile by application_id, never
        dispatch again. This port performs no network call and exposes no apply
        HTTP route. T31 must supply its own durable execution/recovery adapter.
        """
        validate_identifier(approval_id)
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(approval_id=approval_id, actor_id=principal.subject_id)
        key = change_key("application-key", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_access)
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1], False
            saved = _get(c, plan_id, change_key("approval", approval_id))
            if saved is None or _get(c, plan_id, change_key("consumed", approval_id)):
                raise EventError("approval_unavailable")
            approved = saved[1]
            current = self._current(c, principal, plan, now,
                rehearsal_id=approved["run_id"], approved_by=approved["actor_id"], approval_id=approval_id)
            if approved["plan_version"] != plan["version"] or asdict(current) != approved["context"]:
                raise EventError("approval_stale")
            # Re-approving the same verified candidate must not allow another apply.
            candidate_key = change_key("candidate", _canonical(dict(
                rehearsal_id=current.rehearsal_id, verification_id=current.verification_id)))
            if _get(c, plan_id, candidate_key):
                raise EventError("candidate_already_claimed")
            receipt = dict(application_id=str(uuid4()), approval_id=approval_id,
                binding=current.binding, rehearsal_id=current.rehearsal_id,
                verification_id=current.verification_id, context_revision=current.revision,
                claimed_by=principal.subject_id, claimed_at=now)
            _put(c, plan_id, key, request, receipt)
            _put(c, plan_id, change_key("consumed", approval_id), request, receipt)
            _put(c, plan_id, candidate_key, request, receipt)
            return receipt, True
