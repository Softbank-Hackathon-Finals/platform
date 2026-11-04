"""FAILBACK admission and execution journal in the plan transaction (T20, design 6 S4 · 8.2).

The host authority must read the current verified failback context (operator decision, current
plan, the cutover being rolled back, Executor epoch and DB pair) under the same plan lock. No
authority is installed by default. Like CUTOVER, the dispatch claim is committed before the one
POST; retries, response loss and restarts only GET.

Journal keys use the ``failback-`` prefix so the cutover path never reads a failback command.
Epoch records share the cutover key so one plan never reuses an epoch across both kinds.

After a committed DONE the plan moves on only with integrity evidence bound to this failback run
and a confirmed Edge restore (T17). The Edge call is claimed durably first and made outside the
plan lock; completion re-checks the state under the lock. Remote ABORT_PRE_WRITE returns to WATCH.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from .contract import validate_event, validate_identifier
from .cutover import (
    _get,
    _key,
    _put,
    _snapshot,
    latest_observation,
    project_observation,
    unavailable_authority,
)
from .executor import ExecutionRead, ExpectedExecution, _timestamp
from .plan_api import Principal, authorized_plan
from .plans import _canonical
from .store import EventError

COMMAND_FIELDS = {"plan_id", "epoch", "source", "target", "write_pause_budget_ms", "deadline"}


@dataclass(frozen=True)
class CurrentFailback:
    """Verified server record for one failback decision, not a client proof.

    The authority checks the operator's current permission and the plan, and supplies the
    reversed Executor command with a new epoch. Its revision binds those checks and configuration.
    """

    binding: dict
    command: dict
    revision: str


class FailbackAuthority(Protocol):
    def __call__(self, connection, principal: Principal, plan: dict, *,
                 now: str, approved_by: str | None, approval_id: str | None) -> CurrentFailback: ...


@dataclass(frozen=True)
class FailbackIntegrity:
    """Integrity evidence the host read for one failback run (T13 comparison inside the Executor)."""

    receipt_id: str
    command_id: str
    epoch: int
    execution_id: str
    matched: bool


class IntegrityReader(Protocol):
    def __call__(self, connection, plan: dict, *, command_id: str, epoch: int,
                 execution_id: str) -> FailbackIntegrity: ...


class EdgePort(Protocol):
    """Restore user traffic to the origin. Repeating the same arguments must be idempotent.
    Return True only when the restore is confirmed; anything else means not yet confirmed."""

    def restore_origin(self, *, plan_id: str, command_id: str, execution_id: str, epoch: int) -> bool: ...


class UnavailableEdge:
    def restore_origin(self, **_kwargs) -> bool:
        raise EventError("edge_unavailable")


def failback_key(kind: str, value: str) -> str:
    return f"failback-{kind}-" + hashlib.sha256(value.encode()).hexdigest()


def cutover_record(c, plan_id) -> dict:
    """The cutover this plan would roll back: its run and matched integrity receipt."""
    run, checkpoint = cutover_checkpoint(c, plan_id)
    return run | {"integrity_receipt_id": checkpoint["receipt_id"]}


def cutover_checkpoint(c, plan_id) -> tuple[dict, dict]:
    """The committed cutover run and its integrity checkpoint, read from stored records.

    Plan versions move on during FAILBACK and back to WATCH, so neither is found by version
    arithmetic. Exactly one cutover command must be observed as a committed DONE, with exactly one
    matched integrity checkpoint for that run. Anything else raises cutover_record_unavailable.
    """
    done = []
    rows = c.execute("SELECT command_key,receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, "cutover-command-%")).fetchall()
    for row in rows:
        command_id = json.loads(row["receipt_json"]).get("command_id")
        if not command_id or row["command_key"] != _key("command", command_id):
            continue
        snapshot = latest_observation(c, plan_id, command_id)["snapshot"]
        if snapshot and snapshot["remote_status"] == "DONE" and snapshot["committed"] is True:
            done.append((command_id, snapshot))
    if len(done) != 1:
        raise EventError("cutover_record_unavailable")
    command_id, snapshot = done[0]
    run = dict(command_id=command_id, epoch=snapshot["epoch"], execution_id=snapshot["execution_id"])
    checkpoints = []
    for row in c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                         (plan_id, "workflow:%")).fetchall():
        evidence = json.loads(row["receipt_json"])
        result = evidence.get("result")
        if isinstance(result, dict) and set(result) == {"run", "matched"} and result["run"] == run \
                and result["matched"] is True:
            checkpoints.append(evidence)
    if len(checkpoints) != 1:
        raise EventError("cutover_record_unavailable")
    return run, checkpoints[0]


def active_failback(c, plan_id, version) -> str | None:
    """The failback command that moved the plan to FAILBACK at this plan version."""
    stored = _get(c, plan_id, failback_key("active", str(version)))
    return stored[1]["command_id"] if stored else None


def used_epochs(c, plan_id) -> list[int]:
    """Every epoch this plan has claimed (cutover and failback share the epoch records)."""
    rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, "cutover-epoch-%")).fetchall()
    return [json.loads(row["receipt_json"])["command"]["epoch"] for row in rows]


class FailbackService:
    def __init__(self, store, executor, *, bindings: dict[str, str],
                 authority: FailbackAuthority = unavailable_authority,
                 read_integrity: IntegrityReader = unavailable_authority, edge: EdgePort | None = None,
                 check_access=None, check_execute=None, clock=None):
        if (len(bindings) != 2 or len(set(bindings.values())) != 2
                or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in bindings.values())):
            raise ValueError("fixed_database_pair_required")
        for env in bindings:
            validate_identifier(env)
        self.store, self.executor = store, executor
        self.bindings = dict(bindings)
        self.authority = authority
        self.read_integrity = read_integrity
        self.edge = edge or UnavailableEdge()
        self.check_access = check_access
        # Moving a plan on (Edge call, completion, ABORT -> WATCH) is an execution, not a read.
        # The host passes its execute check here, and runs recovery as a principal that holds it.
        self.check_execute = check_execute
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _plan(self, c, principal, plan_id, *, execute=False):
        plan = authorized_plan(c, principal, plan_id, self.check_access)
        if execute and self.check_execute is not None:
            self.check_execute(c, principal, plan)
        return plan

    @staticmethod
    def _owns(c, plan, command_id) -> bool:
        """Only the command that moved the plan to FAILBACK at this version may change its state."""
        return plan["state"] == "FAILBACK" and active_failback(c, plan["plan_id"], plan["version"]) == command_id

    def _current(self, c, principal, plan, now, approved_by=None, approval_id=None):
        if plan["state"] != "WATCH":
            raise EventError("transition_not_allowed")
        try:
            current = self.authority(c, principal, plan, now=now, approved_by=approved_by, approval_id=approval_id)
        except EventError as error:
            if error.code == "forbidden":
                raise
            raise EventError("authority_unavailable") from None
        except Exception:
            raise EventError("authority_unavailable") from None
        if not isinstance(current, CurrentFailback):
            raise EventError("authority_unavailable")
        # Detach mutable provider objects before validation and persistence.
        current = CurrentFailback(**json.loads(_canonical(asdict(current))))
        binding, command = current.binding, current.command
        validate_identifier(current.revision)
        spec = json.loads(plan["spec_json"])
        cutover = cutover_record(c, plan["plan_id"])
        expected = dict(plan_id=plan["plan_id"], plan_digest=spec["plan_digest"],
                        source_environment_id=spec["target_environment_id"],
                        target_environment_id=spec["source_environment_id"],
                        cutover_execution_id=cutover["execution_id"], cutover_epoch=cutover["epoch"],
                        integrity_receipt_id=cutover["integrity_receipt_id"])
        if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in expected.items()):
            raise EventError("binding_mismatch")
        validate_event(dict(schema_version="1.0", event_id=f"{plan['plan_id']}:1", sequence=1,
                            plan_id=plan["plan_id"], run_id="validation", occurred_at=now,
                            event_type="approval.requested", data=dict(
                                approval_id="validation", approval_kind="failback", binding=binding)))
        checked_at = self.clock()
        if _timestamp(binding["expires_at"]) <= _timestamp(checked_at):
            raise EventError("approval_expired")
        if set(command) != COMMAND_FIELDS:
            raise EventError("invalid_command")
        if (command["plan_id"] != plan["plan_id"] or type(command["epoch"]) is not int
                or not 1 <= command["epoch"] <= 2**63 - 1
                or type(command["write_pause_budget_ms"]) is not int
                or not 0 < command["write_pause_budget_ms"] <= 600_000
                or not _timestamp(checked_at) < _timestamp(command["deadline"]) <= _timestamp(binding["expires_at"])):
            raise EventError("invalid_command")
        # The Executor accepts only an epoch above every one it has seen for the resource. Within
        # the plan, that includes the cutover and every earlier (e.g. aborted) failback.
        if command["epoch"] <= max(used_epochs(c, plan["plan_id"]), default=0):
            raise EventError("epoch_not_increasing")
        # Exactly the registered pair, reversed: source is the plan's target and target its source.
        for side, env in (("source", spec["target_environment_id"]), ("target", spec["source_environment_id"])):
            endpoint = command[side]
            if (not isinstance(endpoint, dict) or set(endpoint) != {"environment_id", "connection_ref"}
                    or endpoint["environment_id"] != env or env not in self.bindings
                    or endpoint["connection_ref"] != self.bindings[env]):
                raise EventError("unregistered_database_pair")
        return current

    def approve(self, principal, plan_id, *, binding, expected_version, command_key):
        """Record a human FAILBACK decision. It is not an execution grant by itself."""
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(binding=binding, expected_version=expected_version, actor_id=principal.subject_id)
        key = failback_key("approval-key", command_key)
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
            current = self._current(c, principal, plan, now)
            if _canonical(binding) != _canonical(current.binding):
                raise EventError("binding_mismatch")
            approval_id = str(uuid4())
            receipt = dict(approval_id=approval_id, approval_kind="failback", binding=current.binding,
                           actor_id=principal.subject_id, approved_at=now,
                           plan_version=plan["version"], context=asdict(current))
            _put(c, plan_id, failback_key("approval", approval_id), request, receipt)
            _put(c, plan_id, key, request, receipt)
            # The request and the decision share one approval ID and binding, in one transaction, so the
            # durable history (and SSE replay) links who decided which request.
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=approval_id,
                event_type="approval.requested", occurred_at=now, producer_key=failback_key("requested", approval_id),
                data=dict(approval_id=approval_id, approval_kind="failback", binding=current.binding))
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=approval_id,
                event_type="approval.decided", occurred_at=now, producer_key=failback_key("approved", approval_id),
                data=dict(approval_id=approval_id, approval_kind="failback", binding=current.binding,
                          actor_id=principal.subject_id, decision="approved"))
            return receipt

    def _prepare(self, principal, plan_id, approval_id, command_key):
        validate_identifier(approval_id)
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(approval_id=approval_id, actor_id=principal.subject_id)
        key = failback_key("submit", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_access)
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1], False
            approval = _get(c, plan_id, failback_key("approval", approval_id))
            if approval is None or _get(c, plan_id, failback_key("consumed", approval_id)):
                raise EventError("approval_unavailable")
            approved = approval[1]
            current = self._current(c, principal, plan, now, approved_by=approved["actor_id"], approval_id=approval_id)
            if approved["plan_version"] != plan["version"] or asdict(current) != approved["context"]:
                raise EventError("approval_stale")
            epoch = current.command["epoch"]
            if _get(c, plan_id, _key("epoch", str(epoch))):
                raise EventError("epoch_already_used")
            command_id = str(uuid4())
            receipt = dict(command_id=command_id, approval_id=approval_id, approved_by=approved["actor_id"],
                           command=current.command, context_revision=current.revision, dispatch_claimed_at=now)
            # Commit the dispatch claim BEFORE touching the network. A crash or commit
            # acknowledgement loss here must lead only to GET, never a second POST.
            _put(c, plan_id, key, request, receipt)
            _put(c, plan_id, failback_key("command", command_id), request, receipt)
            _put(c, plan_id, failback_key("consumed", approval_id), request, receipt)
            _put(c, plan_id, _key("epoch", str(epoch)), request, receipt)
            # Recovery finds the running failback by the version this claim creates.
            _put(c, plan_id, failback_key("active", str(plan["version"] + 1)), request, receipt)
            c.execute("UPDATE plans SET state='FAILBACK',version=version+1,updated_at=? WHERE plan_id=?",
                      (now, plan_id))
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
                event_type="state.changed", occurred_at=now, producer_key=failback_key("started", command_id),
                data={"from": "WATCH", "to": "FAILBACK"}, state_owner=True)
            return receipt, True

    def submit(self, principal, plan_id, *, approval_id, command_key):
        record, created = self._prepare(principal, plan_id, approval_id, command_key)
        if not created:
            return self.reconcile(principal, plan_id, record["command_id"])
        expected = ExpectedExecution(plan_id, record["command"]["epoch"])
        read = self.executor.submit(expected, record["command"], idempotency_key=record["command_id"])
        observed = self._project(principal, plan_id, record["command_id"], read)
        return observed | dict(failback=self.advance(principal, plan_id, record["command_id"]))

    def reconcile(self, principal, plan_id, command_id):
        """GET only. Never sends a new remote command."""
        validate_identifier(command_id)
        with self.store._transaction() as c:
            self._plan(c, principal, plan_id, execute=True)
            record = _get(c, plan_id, failback_key("command", command_id))
            if record is None:
                raise EventError("command_not_found")
            previous = _snapshot(latest_observation(c, plan_id, command_id, failback_key)["snapshot"])
        expected = ExpectedExecution(plan_id, record[1]["command"]["epoch"],
                                     previous.execution_id if previous else None)
        read = self.executor.read(expected, previous=previous)
        observed = self._project(principal, plan_id, command_id, read)
        return observed | dict(failback=self.advance(principal, plan_id, command_id))

    def _project(self, principal, plan_id, command_id, read: ExecutionRead):
        # A stored committed DONE is final: a later failed GET must not hold a plan whose DB is
        # already back on the origin (T20 spec criterion 1).
        return project_observation(self.store, principal, plan_id, command_id, read,
                                   check_access=self.check_access, now=self.clock(),
                                   key_for=failback_key, running_state="FAILBACK", hold_after_done=False,
                                   owns=lambda c, plan: self._owns(c, plan, command_id),
                                   authorize=self.check_execute)

    # After the observation ------------------------------------------------------------

    def _record(self, c, plan_id, command_id, record, status, now, **extra):
        data = dict(command_id=command_id, approval_id=record["approval_id"], approved_by=record["approved_by"],
                    status=status, started_at=record["dispatch_claimed_at"]) | extra
        self.store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
            event_type="failback.recorded", occurred_at=now, producer_key=failback_key(status, command_id),
            data=data)

    def _transition(self, c, plan_id, command_id, to_state, now):
        c.execute("UPDATE plans SET state=?,version=version+1,updated_at=? WHERE plan_id=?", (to_state, now, plan_id))
        self.store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
            event_type="state.changed", occurred_at=now, producer_key=failback_key(f"to-{to_state}", command_id),
            data={"from": "FAILBACK", "to": to_state}, state_owner=True)

    def _evidence(self, c, plan, record, snapshot):
        """Integrity evidence bound to this run, or None when it cannot be read yet.
        Raises EventError("integrity_mismatch") when it names another run or does not match."""
        command_id = record["command_id"]
        try:
            evidence = self.read_integrity(c, dict(plan), command_id=command_id, epoch=snapshot["epoch"],
                                           execution_id=snapshot["execution_id"])
        except Exception:  # noqa: BLE001 - missing or unreadable evidence is not a verdict
            return None
        if not isinstance(evidence, FailbackIntegrity):
            return None
        try:
            validate_identifier(evidence.receipt_id)
        except ValueError:
            raise EventError("integrity_mismatch") from None
        if ((evidence.command_id, evidence.epoch, evidence.execution_id)
                != (command_id, snapshot["epoch"], snapshot["execution_id"]) or evidence.matched is not True):
            raise EventError("integrity_mismatch")
        return evidence

    def advance(self, principal, plan_id, command_id):
        """Move a FAILBACK plan on from its stored observation. Never sends an Executor command.

        Returns one of: not_failback, running, aborted, evidence_pending, held, edge_pending, rolled_back.
        not_failback also covers an older command once a newer failback drives the plan.
        """
        validate_identifier(command_id)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = self._plan(c, principal, plan_id, execute=True)
            stored = _get(c, plan_id, failback_key("command", command_id))
            if stored is None:
                raise EventError("command_not_found")
            record = stored[1]
            if _get(c, plan_id, failback_key("rolled-back", command_id)):
                return "rolled_back"
            if not self._owns(c, plan, command_id):
                return "not_failback"
            snapshot = latest_observation(c, plan_id, command_id, failback_key)["snapshot"]
            if snapshot and snapshot["remote_status"] == "ABORT_PRE_WRITE" and snapshot["committed"] is False:
                # The Executor restored the target and opened nothing on the origin (D3).
                self._record(c, plan_id, command_id, record, "aborted", now, finished_at=now,
                             execution_id=snapshot["execution_id"])
                self._transition(c, plan_id, command_id, "WATCH", now)
                return "aborted"
            if not (snapshot and snapshot["remote_status"] == "DONE" and snapshot["committed"] is True):
                return "running"
            claim = _get(c, plan_id, failback_key("edge-claim", command_id))
            if claim is None:
                try:
                    evidence = self._evidence(c, plan, record, snapshot)
                except EventError:
                    self._transition(c, plan_id, command_id, "SAFE_HOLD", now)
                    return "held"
                if evidence is None:
                    return "evidence_pending"
                claim = dict(command_id=command_id, execution_id=snapshot["execution_id"], epoch=snapshot["epoch"],
                             integrity_receipt_id=evidence.receipt_id, claimed_at=now)
                # Claim the Edge call before making it: a crash afterwards retries the same call.
                _put(c, plan_id, failback_key("edge-claim", command_id), {}, claim)
                self._record(c, plan_id, command_id, record, "edge_pending", now,
                             execution_id=claim["execution_id"], integrity_receipt_id=claim["integrity_receipt_id"])
            else:
                claim = claim[1]
        try:
            confirmed = self.edge.restore_origin(plan_id=plan_id, command_id=command_id,
                                                 execution_id=claim["execution_id"], epoch=claim["epoch"]) is True
        except Exception:  # noqa: BLE001 - an Edge failure leaves the plan waiting for the Edge only
            confirmed = False
        if not confirmed:
            return "edge_pending"
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = self._plan(c, principal, plan_id, execute=True)
            if _get(c, plan_id, failback_key("rolled-back", command_id)):
                return "rolled_back"
            if not self._owns(c, plan, command_id):
                return "not_failback"
            recovery_ms = int((_timestamp(now) - _timestamp(record["dispatch_claimed_at"])).total_seconds() * 1000)
            _put(c, plan_id, failback_key("rolled-back", command_id), {}, dict(finished_at=now, recovery_ms=recovery_ms))
            self._record(c, plan_id, command_id, record, "rolled_back", now, finished_at=now,
                         recovery_ms=max(0, recovery_ms), execution_id=claim["execution_id"],
                         integrity_receipt_id=claim["integrity_receipt_id"])
            self._transition(c, plan_id, command_id, "DONE_ROLLED_BACK", now)
            return "rolled_back"
