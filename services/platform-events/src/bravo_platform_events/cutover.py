"""CUTOVER admission and append-only execution journal in the plan transaction.

The host authority must read authenticated T19/SHIFT/permission records under
the same plan lock. No authority is installed by default. This module cannot
advance a plan to SHIFT, synthesize a gate, or turn remote DONE into plan DONE.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from .contract import validate_event, validate_identifier
from .executor import (
    ExecutionRead,
    ExecutionSnapshot,
    ExecutionStep,
    ExpectedExecution,
    _extends,
    _timestamp,
)
from .plan_api import Principal, authorized_plan
from .plans import PlanStore, _canonical
from .store import EventError


@dataclass(frozen=True)
class CurrentCutover:
    """Verified server record, not a client proof or a gate-check implementation.

    The authority checks the entire T5 result against its authenticated immutable
    source, current exceptions/permissions/revocations, security holds, and the
    T17 100%/latency condition. Its revision binds those checks and configuration.
    Every writer that can revoke eligibility must use the same plan lock.
    """

    binding: dict
    command: dict
    revision: str


class CutoverAuthority(Protocol):
    def __call__(self, connection, principal: Principal, plan: dict, *,
                 now: str, approved_by: str | None, approval_id: str | None) -> CurrentCutover: ...


def unavailable_authority(*_args, **_kwargs):
    raise EventError("authority_unavailable")


def _key(kind: str, value: str) -> str:
    return f"cutover-{kind}-" + hashlib.sha256(value.encode()).hexdigest()


def _get(c, plan_id, key):
    row = c.execute("SELECT * FROM plan_commands WHERE plan_id=? AND command_key=?",
                    (plan_id, key)).fetchone()
    if row is None:
        return None
    return json.loads(row["request_json"]), json.loads(row["receipt_json"])


def _put(c, plan_id, key, request, receipt):
    c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
              (plan_id, key, _canonical(request), _canonical(receipt)))


def _snapshot(value):
    if value is None:
        return None
    return ExecutionSnapshot(**{**value, "steps": tuple(ExecutionStep(**s) for s in value["steps"])})


class CutoverService:
    def __init__(self, store: PlanStore, executor, *, bindings: dict[str, str],
                 authority: CutoverAuthority = unavailable_authority, check_access=None, clock=None):
        if (len(bindings) != 2 or len(set(bindings.values())) != 2
                or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in bindings.values())):
            raise ValueError("fixed_database_pair_required")
        for env in bindings:
            validate_identifier(env)
        self.store, self.executor = store, executor
        self.bindings = dict(bindings)
        self.authority = authority
        self.check_access = check_access
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _current(self, c, principal, plan, now, approved_by=None, approval_id=None):
        if plan["state"] != "SHIFT":
            raise EventError("transition_not_allowed")
        try:
            current = self.authority(c, principal, plan, now=now, approved_by=approved_by, approval_id=approval_id)
        except EventError as error:
            if error.code == "forbidden":
                raise
            raise EventError("authority_unavailable") from None
        except Exception:
            raise EventError("authority_unavailable") from None
        if not isinstance(current, CurrentCutover):
            raise EventError("authority_unavailable")
        # Detach mutable provider objects before validation and persistence.
        current = CurrentCutover(**json.loads(_canonical(asdict(current))))
        binding, command = current.binding, current.command
        validate_identifier(current.revision)
        spec = json.loads(plan["spec_json"])
        if any(binding.get(k) != spec[k] for k in
               ("plan_id", "plan_digest", "target_environment_id")):
            raise EventError("binding_mismatch")
        validate_event(dict(schema_version="1.0", event_id=f"{plan['plan_id']}:1", sequence=1,
                            plan_id=plan["plan_id"], run_id="validation", occurred_at=now,
                            event_type="approval.requested", data=dict(
                                approval_id="validation", approval_kind="cutover", binding=binding)))
        checked_at = self.clock()
        if _timestamp(binding["expires_at"]) <= _timestamp(checked_at):
            raise EventError("approval_expired")
        if set(command) != {"plan_id", "epoch", "source", "target", "write_pause_budget_ms", "deadline"}:
            raise EventError("invalid_command")
        if (command["plan_id"] != plan["plan_id"] or type(command["epoch"]) is not int
                or not 1 <= command["epoch"] <= 2**63 - 1
                or type(command["write_pause_budget_ms"]) is not int
                or not 0 < command["write_pause_budget_ms"] <= 600_000
                or not _timestamp(checked_at) < _timestamp(command["deadline"]) <= _timestamp(binding["expires_at"])):
            raise EventError("invalid_command")
        for side in ("source", "target"):
            endpoint = command[side]
            env = spec[f"{side}_environment_id"]
            if (not isinstance(endpoint, dict) or set(endpoint) != {"environment_id", "connection_ref"}
                    or endpoint["environment_id"] != env or env not in self.bindings
                    or endpoint["connection_ref"] != self.bindings[env]):
                raise EventError("unregistered_database_pair")
        if command["source"]["environment_id"] == command["target"]["environment_id"]:
            raise EventError("unregistered_database_pair")
        return current

    def approve(self, principal, plan_id, *, binding, expected_version, command_key):
        """Record a human CUTOVER decision. S2 change approvals have no path here."""
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(binding=binding, expected_version=expected_version,
                       actor_id=principal.subject_id)
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
            current = self._current(c, principal, plan, now)
            if _canonical(binding) != _canonical(current.binding):
                raise EventError("binding_mismatch")
            approval_id = str(uuid4())
            receipt = dict(approval_id=approval_id, approval_kind="cutover", binding=current.binding,
                           actor_id=principal.subject_id, approved_at=now,
                           plan_version=plan["version"], **{"context": asdict(current)})
            _put(c, plan_id, _key("approval", approval_id), request, receipt)
            _put(c, plan_id, key, request, receipt)
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=approval_id,
                event_type="approval.decided", occurred_at=now, producer_key=_key("approved", approval_id),
                data=dict(approval_id=approval_id, approval_kind="cutover", binding=current.binding,
                          actor_id=principal.subject_id, decision="approved"))
            return receipt

    def _prepare(self, principal, plan_id, approval_id, command_key):
        validate_identifier(approval_id)
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        request = dict(approval_id=approval_id, actor_id=principal.subject_id)
        key = _key("submit", command_key)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            now = self.clock()
            plan = authorized_plan(c, principal, plan_id, self.check_access)
            old = _get(c, plan_id, key)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1], False
            approval = _get(c, plan_id, _key("approval", approval_id))
            if approval is None or _get(c, plan_id, _key("consumed", approval_id)):
                raise EventError("approval_unavailable")
            approved = approval[1]
            current = self._current(c, principal, plan, now, approved_by=approved["actor_id"], approval_id=approval_id)
            if approved["plan_version"] != plan["version"] or asdict(current) != approved["context"]:
                raise EventError("approval_stale")
            if _get(c, plan_id, _key("epoch", str(current.command["epoch"]))):
                raise EventError("epoch_already_used")
            command_id = str(uuid4())
            receipt = dict(command_id=command_id, approval_id=approval_id, command=current.command,
                           context_revision=current.revision, dispatch_claimed_at=now)
            # Commit the dispatch claim BEFORE touching the network. A crash or commit
            # acknowledgement loss here must lead only to GET, never a second POST.
            _put(c, plan_id, key, request, receipt)
            _put(c, plan_id, _key("command", command_id), request, receipt)
            _put(c, plan_id, _key("consumed", approval_id), request, receipt)
            _put(c, plan_id, _key("epoch", str(current.command["epoch"])), request, receipt)
            c.execute("UPDATE plans SET state='CUTOVER',version=version+1,updated_at=? WHERE plan_id=?",
                      (now, plan_id))
            self.store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
                event_type="state.changed", occurred_at=now, producer_key=_key("started", command_id),
                data={"from": "SHIFT", "to": "CUTOVER"}, state_owner=True)
            return receipt, True

    def submit(self, principal, plan_id, *, approval_id, command_key):
        record, created = self._prepare(principal, plan_id, approval_id, command_key)
        if not created:
            return self.reconcile(principal, plan_id, record["command_id"])
        expected = ExpectedExecution(plan_id, record["command"]["epoch"])
        read = self.executor.submit(expected, record["command"], idempotency_key=record["command_id"])
        return self._project(principal, plan_id, record["command_id"], read)

    @staticmethod
    def _latest(c, plan_id, command_id):
        return latest_observation(c, plan_id, command_id)

    def reconcile(self, principal, plan_id, command_id):
        validate_identifier(command_id)
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_access)
            record = _get(c, plan_id, _key("command", command_id))
            if record is None:
                raise EventError("command_not_found")
            previous = _snapshot(self._latest(c, plan_id, command_id)["snapshot"])
        expected = ExpectedExecution(plan_id, record[1]["command"]["epoch"],
                                     previous.execution_id if previous else None)
        read = self.executor.read(expected, previous=previous)
        return self._project(principal, plan_id, command_id, read)

    def _project(self, principal, plan_id, command_id, read: ExecutionRead):
        return project_observation(self.store, principal, plan_id, command_id, read,
                                   check_access=self.check_access, now=self.clock(),
                                   key_for=_key, running_state="CUTOVER")


def latest_observation(c, plan_id, command_id, key_for=_key):
    prefix = key_for("observation", command_id)
    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                    "ORDER BY command_key DESC LIMIT 1", (plan_id, prefix + "-%")).fetchone()
    return json.loads(row["receipt_json"]) if row else dict(index=0, snapshot=None, outcome="UNKNOWN")


def project_observation(store, principal, plan_id, command_id, read: ExecutionRead, *, check_access, now,
                        key_for, running_state, hold_after_done=True):
    """Append one Executor observation and its new steps; hold the running plan on UNKNOWN/SAFE_HOLD.

    key_for keeps each command kind's journal separate (cutover-, failback-). running_state is the
    plan state that an uncertain observation moves to SAFE_HOLD. With hold_after_done=False a stored
    committed DONE is final (Executor DONE never changes), so a later failed GET no longer holds.
    """
    with store._transaction(write=True, plan_id=plan_id) as c:
        plan = authorized_plan(c, principal, plan_id, check_access)
        latest = latest_observation(c, plan_id, command_id, key_for)
        previous = _snapshot(latest["snapshot"])
        snapshot = read.snapshot if read.outcome == "OBSERVED" else None
        record = _get(c, plan_id, key_for("command", command_id))
        if record is None:
            raise EventError("command_not_found")
        if snapshot and (snapshot.plan_id != plan_id or snapshot.epoch != record[1]["command"]["epoch"]):
            read, snapshot = ExecutionRead("UNKNOWN", "identity_mismatch"), None
        if snapshot and previous and not _extends(snapshot, previous):
            # Another observer may have committed a newer prefix while GET was pending.
            read, snapshot = ExecutionRead("UNKNOWN", "history_mismatch"), None
        value = dict(outcome=read.outcome, reason=read.reason,
                     snapshot=asdict(snapshot) if snapshot else latest["snapshot"])
        value = json.loads(_canonical(value))
        if all(latest.get(k) == v for k, v in value.items()):
            return dict(command_id=command_id, **value)
        if snapshot:
            for ordinal, step in enumerate(snapshot.steps):
                if previous and ordinal < len(previous.steps):
                    continue
                data = dict(execution_id=snapshot.execution_id, step=step.step, status=step.status)
                if step.elapsed_ms is not None:
                    data["elapsed_ms"] = step.elapsed_ms
                store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
                    event_type="execution.updated", data=data, occurred_at=now,
                    producer_key=key_for("step", f"{snapshot.execution_id}:{ordinal}"))
        settled = (not hold_after_done and previous is not None
                   and previous.remote_status == "DONE" and previous.committed)
        uncertain = read.outcome == "UNKNOWN" or (snapshot and snapshot.remote_status == "SAFE_HOLD")
        if uncertain and plan["state"] == running_state and not settled:
            c.execute("UPDATE plans SET state='SAFE_HOLD',version=version+1,updated_at=? WHERE plan_id=?",
                      (now, plan_id))
            store._append_in_transaction(c, plan_id=plan_id, run_id=command_id,
                event_type="state.changed", occurred_at=now, producer_key=key_for("hold", command_id),
                data={"from": running_state, "to": "SAFE_HOLD"}, state_owner=True)
        index = latest["index"] + 1
        if index > 999_999_999_999:
            raise EventError("observation_limit")
        _put(c, plan_id, f"{key_for('observation', command_id)}-{index:012d}", {}, dict(index=index, **value))
        return dict(command_id=command_id, **value)
