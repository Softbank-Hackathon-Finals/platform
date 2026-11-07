"""Pre-delegated approvals (T49; user decision 2026-11-07 08:57): a person approves a migration's gates once.

The approver records one delegation for a plan with their own verified identity: which gates
(change, shift, cutover, failback), until when (at most 24 h) and, per gate, binding fields that must
match exactly. At a gate only the workflow host turns a live delegation into that approver's principal
and calls the existing approval service. HTTP callers never can. Every check of those services still
applies to the approver at that moment: the current grant for the gate's permission (a grant newer
than the delegation does not count), revocation, requester ≠ approver, binding and expiry.
Each use is recorded once per gate and plan version.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .contract import validate_identifier
from .cutover import _get, _put
from .operator_identity import OperatorPrincipal
from .plan_api import authorized_plan
from .store import EventError

GATES = {"change": "approve_change", "shift": "approve_shift", "cutover": "approve_cutover",
         "failback": "approve_failback"}
MAX_DELEGATION = timedelta(hours=24)
HOST_PRINCIPAL_SECONDS = 60
KEY = "approval-delegation"


def _ts(value):
    return datetime.fromisoformat(value).timestamp()


def _current(c, plan_id):
    """(generation, request, receipt) of the newest delegation; records are insert-only, one per generation."""
    rows = c.execute("SELECT command_key FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, KEY + "-gen-%")).fetchall()
    if not rows:
        return 0, None, None
    generation = max(int(r["command_key"].rsplit("-", 1)[1]) for r in rows)
    request, receipt = _get(c, plan_id, f"{KEY}-gen-{generation}")
    return generation, request, receipt


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ApprovalDelegations:
    def __init__(self, store, access, *, clock=None):
        self.store, self.access = store, access
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def delegate(self, principal, plan_id, *, gates, bounds, expires_at, command_key):
        """The approver's own call. Holding each gate's approve permission now is required."""
        validate_identifier(command_key)
        if (not isinstance(principal, OperatorPrincipal) or type(gates) is not list or not gates
                or len(set(gates)) != len(gates) or set(gates) - set(GATES) or type(bounds) is not dict
                or set(bounds) - set(gates)
                or not all(type(b) is dict and all(type(v) is str for v in b.values()) for b in bounds.values())):
            raise EventError("invalid_delegation")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.access.check_read)
            if not _ts(now) < _ts(expires_at) <= _ts(now) + MAX_DELEGATION.total_seconds():
                raise EventError("invalid_expiry")
            for gate in gates:
                self.access.require(c, principal, plan, GATES[gate])
            request = dict(approver=principal.subject_id, gates=sorted(gates), bounds=bounds,
                           expires_at=expires_at, command_key=command_key)
            generation, old_request, old = _current(c, plan_id)
            if old and not _get(c, plan_id, f"{KEY}-revoked-{old['delegation_id']}"):
                if old_request == request:
                    return old
                raise EventError("delegation_exists")  # revoke first; never widen a live one in place
            receipt = dict(request, delegation_id=str(uuid4()), tenant_id=plan["tenant_id"], created_at=now,
                           authenticated_at=principal.authenticated_at)
            _put(c, plan_id, f"{KEY}-gen-{generation + 1}", request, receipt)
            return receipt

    def revoke(self, principal, plan_id):
        """The approver, or anyone holding all of its gates' approve permissions now."""
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = authorized_plan(c, principal, plan_id, self.access.check_read)
            _, _, receipt = _current(c, plan_id)
            if receipt is None:
                raise EventError("delegation_unavailable")
            if principal.subject_id != receipt["approver"]:
                for gate in receipt["gates"]:
                    self.access.require(c, principal, plan, GATES[gate])
            key = f"{KEY}-revoked-{receipt['delegation_id']}"
            if not _get(c, plan_id, key):
                _put(c, plan_id, key, {}, dict(by=principal.subject_id, at=self.clock()))
            return receipt["delegation_id"]

    def principal_for(self, plan_id, gate, *, binding, version):
        """Host only: the approver's principal for one gate whose binding stays within the delegation."""
        if gate not in GATES or type(binding) is not dict:
            raise EventError("invalid_delegation")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            _, _, receipt = _current(c, plan_id)
            if receipt is None:
                raise EventError("approval_not_delegated")
            if _get(c, plan_id, f"{KEY}-revoked-{receipt['delegation_id']}"):
                raise EventError("delegation_revoked")
            if _ts(now) >= _ts(receipt["expires_at"]):
                raise EventError("delegation_expired")
            if gate not in receipt["gates"]:
                raise EventError("approval_not_delegated")
            if any(binding.get(k) != v for k, v in receipt["bounds"].get(gate, {}).items()):
                raise EventError("delegation_scope_mismatch")
            use = f"{KEY}-use-{gate}-{version}"
            record = dict(delegation_id=receipt["delegation_id"], binding_digest=_digest(binding))
            old = _get(c, plan_id, use)
            if old and old[0] != record:
                raise EventError("delegation_already_used")  # one decision per gate and plan version
            if not old:
                _put(c, plan_id, use, record, dict(record, at=now))
        issued = int(_ts(now))
        expires = min(issued + HOST_PRINCIPAL_SECONDS, int(_ts(receipt["expires_at"])))
        return OperatorPrincipal(receipt["approver"], receipt["tenant_id"], issued, expires,
                                 receipt["authenticated_at"], "delegation:" + receipt["delegation_id"])
