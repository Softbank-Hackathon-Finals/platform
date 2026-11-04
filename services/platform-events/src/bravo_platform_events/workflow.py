"""Resume T10 from a plan ID. Producer authority is an internal read-only port."""

import json
from dataclasses import asdict, dataclass

from .change import change_key
from .contract import validate_identifier
from .cutover import CutoverService, _get, _key, _put, latest_observation, unavailable_authority
from .executor import _timestamp
from .failback import FailbackService, active_failback, cutover_checkpoint, failback_key
from .plan_api import Principal
from .plans import _digest
from .store import EventError


@dataclass(frozen=True)
class Change:
    diff_hash: str
    image_digest: str


@dataclass(frozen=True)
class Analysis:
    scope_digest: str
    inventory_digest: str
    comparison_digest: str


@dataclass(frozen=True)
class Proposal:
    inventory_digest: str
    comparison_digest: str
    change: Change
    schema_valid: bool
    lint_passed: bool


@dataclass(frozen=True)
class Application:
    change: Change
    verification_id: str
    approval_id: str
    application_id: str
    job_passed: bool
    deployed_digest: str


@dataclass(frozen=True)
class Gate:
    change: Change
    gate_id: str
    context_digest: str
    verdict: str


@dataclass(frozen=True)
class ShiftApproval:
    gate: Gate
    approval_id: str
    approved: bool
    expires_at: str


@dataclass(frozen=True)
class Run:
    command_id: str
    epoch: int
    execution_id: str


@dataclass(frozen=True)
class Integrity:
    run: Run
    matched: bool


@dataclass(frozen=True)
class Watch:
    run: Run
    integrity_receipt_id: str
    observation: str
    control_retest: str


@dataclass(frozen=True)
class Evidence:
    plan_id: str
    plan_digest: str
    target_environment_id: str
    version: int
    receipt_id: str
    result: Analysis | Proposal | Application | Gate | ShiftApproval | Integrity | Watch


# No arbitrary state input. Collection and cutover dispatch have existing owners.
STAGES = {
    "ANALYZE": (Analysis, "PROPOSE"),
    "PROPOSE": (Proposal, "REHEARSE"),
    "REHEARSE": (Application, "GATE"),
    "GATE": (Gate, "AWAIT_APPROVAL"),
    "AWAIT_APPROVAL": (ShiftApproval, "SHIFT"),
    "CUTOVER": (Integrity, "WATCH"),
    "WATCH": (Watch, "DONE"),
}


def _checkpoint(version):
    # A colon cannot occur in a client command key. Rows survive SSE retention.
    return f"workflow:{version}"


def _command_for_version(c, plan_id, shift_version):
    """Resolve a pre-existing dispatch claim, including plans started on older builds."""
    rows = c.execute(
        "SELECT command_key,receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
        (plan_id, "cutover-command-%"),
    ).fetchall()
    matches = []
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        command_id = receipt.get("command_id")
        if not command_id or row["command_key"] != _key("command", command_id):
            continue
        approval = _get(c, plan_id, _key("approval", receipt["approval_id"]))
        if approval and approval[1]["plan_version"] == shift_version:
            matches.append(command_id)
    if len(matches) != 1:
        raise EventError("cutover_claim_unavailable")
    return matches[0]


def _observed_run(c, service, plan_id, command_id):
    observation = service._latest(c, plan_id, command_id)
    snapshot = observation["snapshot"]
    if (
        observation["outcome"] == "OBSERVED"
        and snapshot
        and snapshot["remote_status"] == "DONE"
        and snapshot["committed"] is True
    ):
        return Run(command_id, snapshot["epoch"], snapshot["execution_id"])
    return None


def _satisfied(c, proof, spec, previous, run, now):
    result = proof.result
    if isinstance(result, Analysis):
        _digest(result.inventory_digest)
        _digest(result.comparison_digest)
        return result.scope_digest == spec["collection_scope_digest"]
    old = previous["result"] if previous else {}
    if isinstance(result, Proposal):
        for value in (result.change.diff_hash, result.change.image_digest):
            if not value.startswith("sha256:"):
                return False
            _digest(value[7:])
        return (
            result.inventory_digest == old.get("inventory_digest")
            and result.comparison_digest == old.get("comparison_digest")
            and result.schema_valid is True
            and result.lint_passed is True
        )
    if isinstance(result, Application):
        for value in (result.verification_id, result.approval_id, result.application_id):
            validate_identifier(value)
        # T31 reports the remote effect. T10 owns the approval and its one-time
        # claim. Read both under the lock that advances the plan.
        approval = _get(c, proof.plan_id, change_key("approval", result.approval_id))
        consumed = _get(c, proof.plan_id, change_key("consumed", result.approval_id))
        if approval is None or consumed is None:
            return False
        claim = consumed[1]
        return (
            approval[1]["plan_version"] == proof.version
            and claim["application_id"] == result.application_id
            and claim["verification_id"] == result.verification_id
            and claim["binding"]["diff_hash"] == result.change.diff_hash
            and claim["binding"]["image_digest"] == result.change.image_digest
            and asdict(result.change) == old.get("change")
            and result.job_passed is True
            and result.deployed_digest == result.change.image_digest
        )
    if isinstance(result, Gate):
        validate_identifier(result.gate_id)
        _digest(result.context_digest)
        return result.verdict == "PASS" and asdict(result.change) == old.get("change")
    if isinstance(result, ShiftApproval):
        validate_identifier(result.approval_id)
        return (
            result.approved is True
            and asdict(result.gate) == old
            and _timestamp(result.expires_at) > _timestamp(now)
        )
    if isinstance(result, Integrity):
        return run is not None and result.run == run and result.matched is True
    return (
        run is not None
        and result.run == run
        and asdict(run) == old.get("run")
        and result.integrity_receipt_id == previous["receipt_id"]
        and result.observation == "PASS"
        and result.control_retest == "PASS"
    )


class WorkflowRecovery:
    """Internal host driver. The authority resolves *current authenticated* producer
    records, including holds, revocations and freshness, under the shared plan lock.
    It must read locally available authoritative records; network I/O for the
    Executor runs outside this lock. No client endpoint accepts Evidence.
    """

    def __init__(self, cutover: CutoverService, *, read_evidence=unavailable_authority,
                 failback: FailbackService | None = None):
        self.cutover = cutover
        self.read_evidence = read_evidence
        self.failback = failback

    def _plan(self, c, principal, plan_id):
        # Use public host access policy on both sides of the out-of-lock GET.
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        for identifier in (plan_id, principal.tenant_id, principal.subject_id):
            validate_identifier(identifier)
        row = c.execute(
            "SELECT * FROM plans WHERE plan_id=? AND tenant_id=?", (plan_id, principal.tenant_id)
        ).fetchone()
        if row is None:
            raise EventError("forbidden")
        plan = dict(row)
        if self.cutover.check_access is not None:
            self.cutover.check_access(c, principal, plan)
        return plan

    def resume_once(self, principal, plan_id):
        service = self.cutover
        # Read the durable cursor first. Never call submit on this recovery path.
        with service.store._transaction() as c:
            plan = self._plan(c, principal, plan_id)
            state, version = plan["state"], plan["version"]
            if state == "FAILBACK":
                failback_command = active_failback(c, plan_id, version)
            elif state not in STAGES:
                return state
            command_id = None
            if state == "CUTOVER":
                command_id = _command_for_version(c, plan_id, version - 1)
            elif state == "WATCH":
                # A failed (ABORT_PRE_WRITE) failback returns to WATCH at a later version, so the
                # cutover run is found from stored records, not version arithmetic (T20 D3).
                command_id = cutover_checkpoint(c, plan_id)[0]["command_id"]
        if state == "FAILBACK":
            return self._resume_failback(principal, plan_id, failback_command)
        if command_id is not None:
            service.reconcile(principal, plan_id, command_id)

        with service.store._transaction(write=True, plan_id=plan_id) as c:
            plan = self._plan(c, principal, plan_id)
            if (plan["state"], plan["version"]) != (state, version):
                return plan["state"]
            run = _observed_run(c, service, plan_id, command_id) if command_id else None
            if command_id and run is None:
                return state
            if state == "WATCH":
                previous = cutover_checkpoint(c, plan_id)[1]
            else:
                saved = _get(c, plan_id, _checkpoint(version - 1))
                previous = saved[1] if saved else None
            if state not in ("ANALYZE", "CUTOVER") and previous is None:
                raise EventError("checkpoint_unavailable")
            # The reader can locate the correct artifacts using prior receipt and run.
            proof = self.read_evidence(c, plan.copy(), previous, run)
            expected, target = STAGES[state]
            spec = json.loads(plan["spec_json"])
            if (
                not isinstance(proof, Evidence)
                or not isinstance(proof.result, expected)
                or type(proof.version) is not int
                or (proof.plan_id, proof.plan_digest, proof.target_environment_id, proof.version)
                != (plan_id, spec["plan_digest"], spec["target_environment_id"], version)
            ):
                raise EventError("workflow_evidence_unavailable")
            validate_identifier(proof.receipt_id)
            now = service.clock()
            if not _satisfied(c, proof, spec, previous, run, now):
                raise EventError("workflow_condition_unmet")
            _put(c, plan_id, _checkpoint(version), {}, asdict(proof))
            c.execute(
                "UPDATE plans SET state=?,version=version+1,updated_at=? WHERE plan_id=?",
                (target, now, plan_id),
            )
            service.store._append_in_transaction(
                c,
                plan_id=plan_id,
                run_id=proof.receipt_id,
                event_type="state.changed",
                occurred_at=now,
                producer_key=f"workflow-transition-{version}",
                data={"from": state, "to": target},
                state_owner=True,
            )
            return target

    def _resume_failback(self, principal, plan_id, command_id):
        """GET only until a committed DONE is stored; afterwards only evidence and the Edge."""
        if self.failback is None or command_id is None:
            return "FAILBACK"
        service = self.failback
        with service.store._transaction() as c:
            self._plan(c, principal, plan_id)
            snapshot = latest_observation(c, plan_id, command_id, failback_key)["snapshot"]
        if snapshot and snapshot["remote_status"] == "DONE" and snapshot["committed"] is True:
            service.advance(principal, plan_id, command_id)
        else:
            service.reconcile(principal, plan_id, command_id)
        with service.store._transaction() as c:
            return self._plan(c, principal, plan_id)["state"]
