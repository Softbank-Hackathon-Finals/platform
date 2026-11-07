"""Readers and preparers for the stages after PROPOSE (see workflow_host, ADR-0031).

REHEARSE  the approved change is applied once by ApplicationHost (T31), outside the lock;
          ApplicationEvidence reads the outcome under the lock.
GATE      GateRecheck asks T19 after the real APPLIED outcome, outside the lock;
          ApplicationEvidence's GATE branch reads the PASS under the lock.
AWAIT_APPROVAL  a human shift approval (ShiftApprovalService) of the GATE checkpoint.
SHIFT     owned by the operator cutover API (approve + submit); the host does not drive it.
CUTOVER   Integrity from the observed Executor run: DONE, committed, compare and commit succeeded
          (the Executor aborts before commit on any T13 mismatch).
WATCH     T36 SLI judgement of the latest window plus the T19 control retest, both PASS.
ANALYZE   a deployment port over T16/T14 records; there is no default.
Every port without a deployment value is absent, so its state holds (fail closed).
"""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .application_host import ApplicationEvidence, ApplicationHost, GateRecheck
from .contract import validate_identifier
from .cutover import _get, _put, latest_observation
from .plan_api import Principal, authorized_plan
from .store import EventError
from .workflow import Evidence, Gate, Integrity, ShiftApproval, Watch

MAX_SHIFT_APPROVAL = timedelta(hours=24)


def _evidence(plan, receipt_id, result):
    spec = json.loads(plan["spec_json"])
    return Evidence(plan["plan_id"], spec["plan_digest"], spec["target_environment_id"], plan["version"],
                    receipt_id, result)


def _shift_key(kind, value):
    return f"shift-{kind}-{value}"


# --- REHEARSE / GATE -------------------------------------------------------------------------

def _approved_change(store, plan_id, version):
    """The S2 change approval this plan version proceeds with.

    An approval whose application was already claimed wins (a restart resumes it). Otherwise the
    newest approval that an operator has not revoked; re-approval after a refusal is normal.
    """
    from .change import change_key as _ck

    with store._transaction() as c:
        rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                         (plan_id, "change-approval-%")).fetchall()
        found = [json.loads(r["receipt_json"]) for r in rows]
        found = [r for r in found if r.get("plan_version") == version and "approval_id" in r]
        claimed = [r for r in found if _get(c, plan_id, _ck("consumed", r["approval_id"]))]
        if claimed:
            return claimed[0]
        live = [r for r in found if not _get(c, plan_id, _ck("operator-revoked-approval", r["approval_id"]))]
    return max(live, key=lambda r: r["approved_at"]) if live else None


def _delegated_change(changes, delegations, plan_id, version):
    """REHEARSE with a pre-delegated change approval (T49): approve the current verified candidate of this
    version through the same service, as the approver. None: nothing delegated or no candidate yet."""
    from .change_intake import ChangeIntake

    with changes.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone())
        rows = c.execute("SELECT request_json, receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                         (plan_id, "change-intake-proof-%")).fetchall()
        proofs = [(json.loads(r["receipt_json"])["attempt"], json.loads(r["request_json"])) for r in rows]
        proofs = [(attempt, record) for attempt, record in proofs if record["plan_version"] == version]
        if not proofs:
            return None
        # ponytail: the highest attempt across runs is the newest candidate; one rehearsal run per version today.
        run_id = max(proofs, key=lambda p: p[0])[1]["candidate"]["run_id"]
        try:
            current = ChangeIntake.current(c, None, plan, rehearsal_id=run_id)
        except EventError:
            return None  # held or superseded: a person decides
    try:
        principal = delegations.principal_for(plan_id, "change", binding=current.binding, version=version)
    except EventError as error:
        if error.code == "approval_not_delegated":
            return None
        raise
    return changes.approve(principal, plan_id, run_id=run_id, binding=current.binding, expected_version=version,
                           command_key=f"delegated-change-{version}")


def host_principal(subject_id, tenant_id, *, lifetime=300, clock=time.time):
    """The workflow host's own operator identity for applying an approved change.

    It needs its own `apply_change` grant (set_permissions); the change's approver is still checked
    as `approved_by` with `approve_change`, so approval and application stay separate people/actors.
    """
    from .operator_identity import OperatorPrincipal

    now = int(clock())
    return OperatorPrincipal(subject_id, tenant_id, now, now + lifetime, now, "workflow-host")


def application_stages(host: ApplicationHost, recheck: GateRecheck | None, *, receipts_dir, check_current,
                       load_gate, host_subject, delegations=None):
    """Preparers for REHEARSE and (with a T19 recheck) GATE. `host` is this process's single instance."""
    if (not isinstance(host, ApplicationHost) or not (recheck is None or isinstance(recheck, GateRecheck))
            or not isinstance(host_subject, str) or not host_subject):
        raise ValueError("application_ports_required")
    store = host.changes.store

    def reader(principal, outcome):
        return ApplicationEvidence(principal=principal, application_id=outcome.application_id,
                                   receipts_dir=receipts_dir, records=recheck and recheck.records,
                                   check_current=check_current, load_gate=load_gate,
                                   fallback=_unwired)

    def applied(plan_id, tenant_id, version, *, rehearsing=False):
        approval = _approved_change(store, plan_id, version)
        if approval is None and rehearsing and delegations is not None:
            approval = _delegated_change(host.changes, delegations, plan_id, version)
        if approval is None:
            raise EventError("change_approval_required")  # S2 approval is a human decision
        principal = host_principal(host_subject, tenant_id)
        outcome = host.start(principal, plan_id, approval_id=approval["approval_id"],
                             command_key="workflow-apply-" + approval["approval_id"])  # replay is read-only
        if outcome is None:
            raise EventError("application_outcome_unobserved")  # claimed, never re-applied
        return principal, outcome

    def rehearse(plan_id, tenant_id, version):
        return reader(*applied(plan_id, tenant_id, version, rehearsing=True))

    def gate(plan_id, tenant_id, version):
        principal, outcome = applied(plan_id, tenant_id, version - 1)
        try:
            recheck.evaluate(principal, plan_id, outcome)
        except EventError as error:
            # Already evaluated in an earlier sweep: the reader below still requires the stored PASS.
            if error.code != "gate_record_conflict":
                raise
        return reader(principal, outcome)

    return {"REHEARSE": rehearse} | ({"GATE": gate} if recheck else {})


def _unwired(*_):
    raise EventError("workflow_reader_unwired")


# --- AWAIT_APPROVAL ----------------------------------------------------------------------------

class ShiftApprovalService:
    """A human approves the GATE checkpoint for SHIFT. The host only reads the decision."""

    def __init__(self, store, *, check_approve, clock=None):
        if not callable(check_approve):
            raise ValueError("shift_approval_permission_required")
        self.store, self.check_approve = store, check_approve
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def approve(self, principal, plan_id, *, expected_version, expires_at, command_key):
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(expected_version=expected_version, expires_at=expires_at, actor_id=principal.subject_id)
        key = _shift_key("approval-key", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_approve)
            old = _get(c, plan_id, key)
            if old:
                if old[0] != request:
                    raise EventError("command_key_conflict")
                return old[1]
            if plan["state"] != "AWAIT_APPROVAL" or expected_version != plan["version"]:
                raise EventError("version_conflict")
            expires = datetime.fromisoformat(expires_at)
            if not datetime.fromisoformat(now) < expires <= datetime.fromisoformat(now) + MAX_SHIFT_APPROVAL:
                raise EventError("invalid_expiry")
            gate = _get(c, plan_id, f"workflow:{plan['version'] - 1}")
            if gate is None:
                raise EventError("checkpoint_unavailable")
            approval_id = str(uuid4())
            receipt = dict(approval_id=approval_id, approval_kind="shift", plan_version=plan["version"],
                           gate=gate[1]["result"], actor_id=principal.subject_id, approved_at=now,
                           expires_at=expires_at)
            _put(c, plan_id, key, request, receipt)
            _put(c, plan_id, _shift_key("approval", str(plan["version"])), request, receipt)
            # ponytail: no approval.decided event; its schema has no "shift" kind yet (T10 events v1).
            # The AWAIT_APPROVAL -> SHIFT state.changed event is the public record.
            return receipt


def delegated_shift_preparer(service, delegations, *, ttl=timedelta(hours=1)):
    """AWAIT_APPROVAL with a pre-delegated SHIFT approval (T49): the host records the approver's decision once
    per plan version through the same service and permission check; without a delegation a person approves."""
    def prepare(plan_id, _tenant_id, version):
        try:
            principal = delegations.principal_for(plan_id, "shift", binding={"plan_version": str(version)},
                                                  version=version)
        except EventError as error:
            if error.code == "approval_not_delegated":
                return shift_approval_reader
            raise
        expires_at = (datetime.fromisoformat(service.clock()) + ttl).isoformat()
        service.approve(principal, plan_id, expected_version=version, expires_at=expires_at,
                        command_key=f"delegated-shift-{version}")
        return shift_approval_reader

    return prepare


def shift_approval_reader(c, plan, previous, run):
    saved = _get(c, plan["plan_id"], _shift_key("approval", str(plan["version"])))
    if saved is None:
        raise EventError("shift_approval_required")
    receipt = saved[1]
    gate = receipt["gate"]
    result = ShiftApproval(Gate(**{**gate, "change": _change(gate["change"])}), receipt["approval_id"], True,
                           receipt["expires_at"])
    return _evidence(plan, receipt["approval_id"], result)


def _change(value):
    from .workflow import Change

    return Change(**value)


# --- CUTOVER / WATCH ---------------------------------------------------------------------------

def integrity_reader(c, plan, previous, run):
    if run is None:
        raise EventError("cutover_run_unobserved")
    snapshot = latest_observation(c, plan["plan_id"], run.command_id)["snapshot"] or {}
    steps = {s["step"]: s["status"] for s in snapshot.get("steps", [])}
    matched = (snapshot.get("remote_status") == "DONE" and snapshot.get("committed") is True
               and steps.get("compare") == "succeeded" and steps.get("commit") == "succeeded")
    return _evidence(plan, "integrity-" + run.execution_id, Integrity(run, matched))


def pending_retest(_store):
    """WATCH `retest` port until T14 ships a post-cutover control retest: always UNKNOWN.

    ponytail: WATCH records the T36 verdict but never reaches DONE on SLI alone; a FAIL still leads
    to the operator's FAILBACK. Replace with the T14 factory when it exists.
    """
    return lambda c, plan, run: "UNKNOWN"


def watch_reader(*, observe, retest):
    """observe(c, plan, run) and retest(c, plan, run) return PASS/FAIL/UNKNOWN from T36 and T19."""
    if not callable(observe) or not callable(retest):
        raise ValueError("watch_ports_required")

    def read(c, plan, previous, run):
        if run is None or previous is None:
            raise EventError("cutover_run_unobserved")
        result = Watch(run, previous["receipt_id"], observe(c, plan, run), retest(c, plan, run))
        return _evidence(plan, f"watch-{run.execution_id}-{plan['version']}", result)

    return read


def sli_observer(profile, *, clock=None):
    """T36 WATCH judgement of the newest stored window, observed from the cutover run's last update."""
    from bravo_observability.sli import judge

    from .window_store import WindowIntake

    clock = clock or (lambda: datetime.now(UTC).isoformat())

    def observe(c, plan, run):
        snapshot = latest_observation(c, plan["plan_id"], run.command_id)["snapshot"]
        window = WindowIntake.latest_in_transaction(c, plan["plan_id"])
        # ponytail: prior_unknown=0; a run of UNKNOWNs only keeps WATCH waiting, it never passes.
        return judge(profile, window, plan_id=plan["plan_id"], state="WATCH", weight_target=100,
                     observe_from=snapshot["updated_at"], decided_at=clock(), prior_unknown=0)["verdict"]

    return observe
