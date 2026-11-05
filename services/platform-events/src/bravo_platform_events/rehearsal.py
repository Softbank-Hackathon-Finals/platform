"""T10 owns rehearsal admission. The T15 host submits once and reconciles by read."""

import json
import re
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import uuid4

from .contract import validate_identifier
from .cutover import _get, _put
from .plan_api import authorized_plan
from .plans import _digest, change_key
from .store import EventError


@dataclass(frozen=True)
class RehearsalTask:
    source_sha: str
    task_digest: str


@dataclass(frozen=True)
class Admission:
    plan_id: str
    tenant_id: str
    plan_version: int
    task_id: str
    rehearsal_id: str
    attempt: int
    source_sha: str
    task_digest: str

    def receipt(self):
        return {key: getattr(self, key) for key in ("rehearsal_id", "attempt", "task_id", "plan_version")}


@dataclass(frozen=True)
class Observation:
    admission: Admission
    verification_id: str
    proposal_digest: str
    namespace: str
    created_uid: str
    removed_uid: str
    absence_confirmed: bool
    verdict: str


class RehearsalHost(Protocol):
    def task(self, connection, plan: dict, task_id: str) -> RehearsalTask:
        """Read an eligible task from this plan's authenticated local proposal."""

    def submit(self, admission: Admission) -> None:
        """Submit the exact committed intent to T15 once."""

    def observe(self, admission: Admission) -> Observation | None:
        """Read authenticated remote effects and cleanup for this exact attempt."""


def _attempt_key(version, task_id, attempt):
    # Client identifiers exclude colons. A task ID cannot escape its journal prefix.
    return f"rehearsal:{version}:{task_id}:{attempt}"


def _result_key(run, attempt):
    return f"rehearsal-result:{run}:{attempt}"


def _valid_attempt(attempt):
    if type(attempt) is not int or not 1 <= attempt <= 3:
        raise ValueError("invalid_rehearsal_attempt")


def admitted_attempt(c, plan_id, run, attempt):
    """Return None for historical untracked runs; reject an unadmitted attempt on a managed run."""
    index = _get(c, plan_id, "rehearsal-run:" + run)
    if not index:
        return None
    data = index[1]
    slot = _get(c, plan_id, _attempt_key(data["version"], data["task_id"], attempt))
    if not slot:
        raise EventError("rehearsal_unknown")
    return Admission(**slot[1])


def _managed(c, plan):
    return _get(c, plan["plan_id"], f"rehearsal-mode:{plan['version']}") is not None


def _enable_managed(c, plan):
    if _managed(c, plan):
        return
    # Don't strand an authenticated pre-upgrade candidate or an in-flight approval.
    # The journal is append-only. Inspect legacy records only at first admission.
    rows = c.execute("SELECT command_key,request_json,receipt_json FROM plan_commands WHERE plan_id=? "
                     "AND (command_key LIKE ? OR command_key LIKE ?)",
                     (plan["plan_id"], "change-intake-proof-%", "change-approval-%")).fetchall()
    for row in rows:
        field = "receipt_json" if row["command_key"].startswith("change-approval-") else "request_json"
        if json.loads(row[field]).get("plan_version") == plan["version"]:
            raise EventError("rehearsal_mode_conflict")
    _put(c, plan["plan_id"], f"rehearsal-mode:{plan['version']}", {}, {"managed": True})


def require_current_run(c, plan, run, verification_id):
    """Enforce admission even when a host supplies a custom change authority."""
    if not _managed(c, plan):
        return
    p = plan["plan_id"]
    use = _get(c, p, "rehearsal-verification:" + verification_id)
    if use and use[1]["rehearsal_id"] == run:
        number = use[1]["attempt"]
        admission = admitted_attempt(c, p, run, number)
        result = _get(c, p, _result_key(run, number))
        if (admission and admission.plan_version == plan["version"] and result[1]["status"] == "PASS"
                and not _get(c, p, change_key("intake-hold", verification_id))):
            return _get(c, p, change_key("intake-proof", verification_id))[0]
    raise EventError("rehearsal_incomplete")


def require_admitted_delivery(c, plan, run, attempt):
    if _managed(c, plan):
        admitted = admitted_attempt(c, plan["plan_id"], run, attempt)
        if not admitted or admitted.plan_version != plan["version"]:
            raise EventError("rehearsal_unknown")


def require_rehearsals_complete(c, plan):
    """Caller holds the same write lock as admission. Older plans have no rows."""
    rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                     "ORDER BY command_key", (plan["plan_id"], f"rehearsal:{plan['version']}:%")).fetchall()
    latest = {}
    for row in rows:
        admission = Admission(**json.loads(row["receipt_json"]))
        latest[admission.task_id] = admission  # numeric suffix is a single digit (1..3)
    for admission in latest.values():
        result = _get(c, admission.plan_id, _result_key(admission.rehearsal_id, admission.attempt))
        if not result or result[1]["status"] != "PASS":
            raise EventError("rehearsal_incomplete")
        if _get(c, admission.plan_id, change_key("intake-hold", result[1]["verification_id"])):
            raise EventError("verification_held")


class RehearsalService:
    def __init__(self, store, *, host: RehearsalHost, check_start):
        if not callable(check_start) or not all(callable(getattr(host, op, None)) for op in ("task", "submit", "observe")):
            raise ValueError("rehearsal_host_required")
        self.store, self.host, self.check_start = store, host, check_start

    def start(self, principal, plan_id, *, task_id, expected_version, attempt, command_key):
        for value in (plan_id, task_id, command_key):
            validate_identifier(value)
        _valid_attempt(attempt)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid_version")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = authorized_plan(c, principal, plan_id, self.check_start)
            request = dict(task_id=task_id, expected_version=expected_version, attempt=attempt,
                           subject_id=principal.subject_id)
            key = "rehearsal-key:" + command_key
            old = _get(c, plan_id, key)
            if old:
                if old[0] != request:
                    raise EventError("command_key_conflict")
                return old[1]
            if plan["version"] != expected_version or plan["state"] != "REHEARSE":
                raise EventError("rehearsal_not_allowed")
            try:
                task = self.host.task(c, plan, task_id)
                if type(task) is not RehearsalTask or not re.fullmatch("[a-f0-9]{40}", task.source_sha):
                    raise ValueError("invalid_source")
                _digest(task.task_digest)
            except EventError:
                raise
            except Exception:
                raise EventError("authority_unavailable") from None
            _enable_managed(c, plan)
            slot = _attempt_key(expected_version, task_id, attempt)
            existing = _get(c, plan_id, slot)
            if existing:
                admission = Admission(**existing[1])
                if (admission.source_sha, admission.task_digest) != (task.source_sha, task.task_digest):
                    raise EventError("rehearsal_task_changed")
            else:
                run = self._retry_run(c, plan, task_id, task, attempt) if attempt > 1 else uuid4().hex
                admission = Admission(plan_id, principal.tenant_id, expected_version, task_id, run, attempt,
                                      task.source_sha, task.task_digest)
                _put(c, plan_id, slot, {}, asdict(admission))
                if attempt == 1:
                    _put(c, plan_id, "rehearsal-run:" + run, {}, {"task_id": task_id, "version": expected_version})
            receipt = admission.receipt()
            _put(c, plan_id, key, request, receipt)
        if not existing:
            # Commit must precede I/O. A lost ACK or a dead sender requires readback,
            # even if the caller retries the POST under a different idempotency key.
            try:
                self.host.submit(admission)
            except Exception:
                raise EventError("rehearsal_submission_unknown") from None
        return receipt

    @staticmethod
    def _retry_run(c, plan, task_id, task, attempt):
        p = plan["plan_id"]
        prior = _get(c, p, _attempt_key(plan["version"], task_id, attempt - 1))
        if prior:
            admission = Admission(**prior[1])
            outcome = _get(c, p, _result_key(admission.rehearsal_id, attempt - 1))
            if (outcome and outcome[1]["status"] == "FAIL"
                    and _get(c, p, change_key("intake-hold", outcome[1]["verification_id"]))
                    and (task.source_sha, task.task_digest) == (admission.source_sha, admission.task_digest)):
                return admission.rehearsal_id
        raise EventError("rehearsal_retry_unavailable")

    def _read(self, c, principal, plan_id, run, attempt):
        authorized_plan(c, principal, plan_id, self.check_start)
        admission = admitted_attempt(c, plan_id, run, attempt)
        if admission is None:
            raise EventError("rehearsal_unknown")
        return admission

    def reconcile(self, principal, plan_id, *, rehearsal_id, attempt):
        validate_identifier(rehearsal_id)
        _valid_attempt(attempt)
        key = _result_key(rehearsal_id, attempt)
        with self.store._transaction() as c:
            admission = self._read(c, principal, plan_id, rehearsal_id, attempt)
            old = _get(c, plan_id, key)
            if old:
                return old[1]
        try:
            seen = self.host.observe(admission)
        except Exception:
            raise EventError("authority_unavailable") from None
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            self._read(c, principal, plan_id, rehearsal_id, attempt)
            old = _get(c, plan_id, key)
            if old:
                return old[1]
            if seen is None:
                return admission.receipt() | {"status": "UNCONFIRMED"}
            if (not isinstance(seen, Observation) or seen.admission != admission
                    or seen.verdict not in ("PASS", "FAIL") or seen.absence_confirmed is not True
                    or seen.created_uid != seen.removed_uid):
                raise EventError("rehearsal_observation_mismatch")
            try:
                for value in (seen.verification_id, seen.namespace, seen.created_uid):
                    validate_identifier(value)
                _digest(seen.proposal_digest)
            except ValueError:
                raise EventError("rehearsal_observation_mismatch") from None
            if seen.verdict == "PASS":
                self._check_pass(c, admission, seen)
            use = "rehearsal-verification:" + seen.verification_id
            if _get(c, plan_id, use):
                raise EventError("verification_conflict")
            result = admission.receipt() | {"status": seen.verdict, "verification_id": seen.verification_id}
            _put(c, plan_id, key, asdict(seen), result)
            _put(c, plan_id, use, {}, {"rehearsal_id": rehearsal_id, "attempt": attempt})
            return result

    @staticmethod
    def _check_pass(c, admission, seen):
        p, v = admission.plan_id, seen.verification_id
        saved = _get(c, p, change_key("intake-proof", v))
        if saved and not _get(c, p, change_key("intake-hold", v)):
            record = saved[0]
            candidate = record["candidate"]
            if (record["tenant_id"] == admission.tenant_id and record["plan_version"] == admission.plan_version
                    and record["created_uid"] == seen.created_uid and candidate["namespace"] == seen.namespace
                    and candidate["run_id"] == admission.rehearsal_id and candidate["attempt"] == admission.attempt
                    and candidate["proposal_digest"] == seen.proposal_digest):
                return
        raise EventError("rehearsal_verification_unavailable")
