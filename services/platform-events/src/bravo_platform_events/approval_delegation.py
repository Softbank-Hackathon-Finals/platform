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
        """The approver's own call. Holding each gate's approve permission now is required.

        `bounds` pins binding fields a gate's decision must match exactly. A change bound never needs the
        candidate digest: it does not exist yet when the approver delegates (unattended S2). The change
        decision is still only for the version's current T15-verified, unheld candidate, and GATE rechecks
        the applied image before SHIFT (workflow_stages._delegated_change)."""
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
        """Host only: the approver's principal for one gate whose binding stays within the delegation.

        `binding` is the decision's binding, or `binding(c, principal)` computing it in this transaction."""
        if gate not in GATES or not (type(binding) is dict or callable(binding)):
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
            issued = int(_ts(now))
            principal = OperatorPrincipal(receipt["approver"], receipt["tenant_id"], issued,
                                          min(issued + HOST_PRINCIPAL_SECONDS, int(_ts(receipt["expires_at"]))),
                                          receipt["authenticated_at"], "delegation:" + receipt["delegation_id"])
            if callable(binding):
                binding = binding(c, principal)
                if type(binding) is not dict:
                    raise EventError("invalid_delegation")
            if any(binding.get(k) != v for k, v in receipt["bounds"].get(gate, {}).items()):
                raise EventError("delegation_scope_mismatch")
            use = f"{KEY}-use-{gate}-{version}"
            record = dict(delegation_id=receipt["delegation_id"], binding_digest=_digest(binding))
            old = _get(c, plan_id, use)
            if old and old[0] != record:
                raise EventError("delegation_already_used")  # one decision per gate and plan version
            if not old:
                _put(c, plan_id, use, record, dict(record, at=now))
        return principal


def watch_fail_key(version):
    return f"{KEY}-watch-fail-{version}"


class DelegatedGates:
    """T10 operator process only (it holds the Executor command token): the cutover and failback decisions the
    approver delegated, approved and submitted as that approver with their current permissions.

    Cutover waits for the Edge at 100% with a PASS this SHIFT (operator_ports.edge_at_full); failback only for a WATCH FAIL the workflow host recorded at
    this version. Anything unmet or refused is a wait: the next sweep or a person on the same routes decides.
    """

    def __init__(self, store, delegations, *, cutover, failback=None, shift=None):
        self.store, self.delegations, self.cutover, self.failback = store, delegations, cutover, failback
        self.shift = shift  # DelegatedShift: SHIFT weights before the cutover (demo), optional

    def sweep(self):
        from .operator_ports import (
            edge_at_full,  # the cutover authority's own check (T17 100% and SLI PASS)
        )

        with self.store._transaction() as c:
            plans = [dict(r) for r in c.execute(
                "SELECT plan_id, state, version, updated_at FROM plans WHERE state IN ('SHIFT', 'WATCH')").fetchall()]
            due = [(p, "cutover", self.cutover) for p in plans if p["state"] == "SHIFT" and edge_at_full(c, p)]
            due += [(p, "failback", self.failback) for p in plans if p["state"] == "WATCH" and self.failback
                    and _get(c, p["plan_id"], watch_fail_key(p["version"]))]
            shifting = [p for p in plans if self.shift and p["state"] == "SHIFT" and not edge_at_full(c, p)
                        and "shift" in ((_current(c, p["plan_id"])[2] or {}).get("gates") or [])]
        decided = {p["plan_id"]: {"shift": self.shift(p)} for p in shifting}
        for plan, gate, service in due:
            try:
                decided[plan["plan_id"]] = self._decide(plan, gate, service)
            except EventError as error:
                decided[plan["plan_id"]] = {"held": error.code}
        return decided

    def _decide(self, plan, gate, service):
        plan_id, version, chosen = plan["plan_id"], plan["version"], {}

        def current(c, principal):
            row = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone())
            chosen["binding"] = service._current(c, principal, row, service.clock()).binding
            return chosen["binding"]

        principal = self.delegations.principal_for(plan_id, gate, binding=current, version=version)
        approval = service.approve(principal, plan_id, binding=chosen["binding"], expected_version=version,
                                   command_key=f"delegated-{gate}-{version}")
        return service.submit(principal, plan_id, approval_id=approval["approval_id"],
                              command_key=f"delegated-{gate}-submit-{version}")


class DelegatedShift:
    """SHIFT 10 -> 50 -> 100% as the delegated approver (T49 demo), in the operator process that holds the
    Edge journal and LB token: register this version's run, then sequence and hold at 100% for the cutover.

    One thread per plan version. The Edge journal lock keeps it single beside an operator's own run; T36
    still decides every advance and an SLI failure still restores 0% (bravo_edge_control.ShiftRuntime).
    ponytail: one attempt per version; a stopped run is rerun by a person (bravo_edge_control.run).
    """

    def __init__(self, store, delegations, *, provider, journal, profile, load_session):
        self.store, self.delegations, self.provider = store, delegations, provider
        self.journal, self.profile, self.load_session = journal, profile, load_session
        self.runs = {}

    def __call__(self, plan):
        import threading

        key = (plan["plan_id"], plan["version"])
        if key in self.runs:
            return "running" if self.runs[key].is_alive() else "finished"
        self.runs[key] = threading.Thread(target=self.run, args=key, name="t49-delegated-shift", daemon=True)
        self.runs[key].start()
        return "started"

    def run(self, plan_id, version):
        import logging
        from threading import Event

        from bravo_edge_control.authority import ShiftAuthority
        from bravo_edge_control.control import _now
        from bravo_edge_control.host import ShiftHost
        from bravo_edge_control.runtime import ShiftRuntime

        log = logging.getLogger(__name__)
        run_id = f"t49-{plan_id}-v{version}"

        def principal():
            return self.delegations.principal_for(plan_id, "shift", binding={"edge_run": run_id}, version=version)

        authority = ShiftAuthority(self.store, run_id=run_id, load_session=self.load_session, clock=_now)
        host = ShiftHost(self.store, self.provider, self.journal, authority=authority, clock=_now)
        try:
            try:
                authority.register(principal(), plan_id, run_id=run_id, profile=self.profile,
                                   binding=self.provider.binding)
            except Exception:  # noqa: BLE001  (registered by an earlier attempt; sequence rechecks it)
                log.info("delegated shift %s: registration kept", plan_id)
            result = ShiftRuntime(host, principal=principal, plan_id=plan_id, run_id=run_id).sequence(
                stop=Event(), hold_at_full=True)
            log.info("delegated shift %s: %s", plan_id, result.get("status"))
            return result
        except Exception as error:  # noqa: BLE001  (the plan stays in SHIFT; a person decides)
            log.info("delegated shift %s stopped: %s", plan_id, getattr(error, "code", type(error).__name__))
            return None
