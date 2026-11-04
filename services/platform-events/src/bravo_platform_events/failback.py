"""FAILBACK admission and execution journal in the plan transaction (T20, design 6 S4 · 8.2).

The host authority must read the current verified failback context (operator decision, current
plan, the cutover being rolled back, Executor epoch and DB pair) under the same plan lock. No
authority is installed by default. Like CUTOVER, the dispatch claim is committed before the one
POST; retries, response loss and restarts only GET.

Journal keys use the ``failback-`` prefix so the cutover path never reads a failback command.
Epoch records share the cutover key so one plan never reuses an epoch across both kinds.
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


def failback_key(kind: str, value: str) -> str:
    return f"failback-{kind}-" + hashlib.sha256(value.encode()).hexdigest()


def cutover_record(c, plan_id) -> dict:
    """The cutover this plan would roll back, read from stored records (not version arithmetic).

    Exactly one cutover command must be observed as a committed DONE, with exactly one matched
    integrity checkpoint for that run. Anything else is not a failback candidate.
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
    receipts = []
    for row in c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                         (plan_id, "workflow:%")).fetchall():
        evidence = json.loads(row["receipt_json"])
        result = evidence.get("result")
        if isinstance(result, dict) and set(result) == {"run", "matched"} and result["run"] == run \
                and result["matched"] is True:
            receipts.append(evidence["receipt_id"])
    if len(receipts) != 1:
        raise EventError("cutover_record_unavailable")
    return run | {"integrity_receipt_id": receipts[0]}


def used_epochs(c, plan_id) -> list[int]:
    """Every epoch this plan has claimed (cutover and failback share the epoch records)."""
    rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, "cutover-epoch-%")).fetchall()
    return [json.loads(row["receipt_json"])["command"]["epoch"] for row in rows]


class FailbackService:
    def __init__(self, store, executor, *, bindings: dict[str, str],
                 authority: FailbackAuthority = unavailable_authority, check_access=None, clock=None):
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
        return self._project(principal, plan_id, record["command_id"], read)

    def reconcile(self, principal, plan_id, command_id):
        """GET only. Never sends a new remote command."""
        validate_identifier(command_id)
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_access)
            record = _get(c, plan_id, failback_key("command", command_id))
            if record is None:
                raise EventError("command_not_found")
            previous = _snapshot(latest_observation(c, plan_id, command_id, failback_key)["snapshot"])
        expected = ExpectedExecution(plan_id, record[1]["command"]["epoch"],
                                     previous.execution_id if previous else None)
        read = self.executor.read(expected, previous=previous)
        return self._project(principal, plan_id, command_id, read)

    def _project(self, principal, plan_id, command_id, read: ExecutionRead):
        # A stored committed DONE is final: a later failed GET must not hold a plan whose DB is
        # already back on the origin (T20 spec criterion 1).
        return project_observation(self.store, principal, plan_id, command_id, read,
                                   check_access=self.check_access, now=self.clock(),
                                   key_for=failback_key, running_state="FAILBACK", hold_after_done=False)
