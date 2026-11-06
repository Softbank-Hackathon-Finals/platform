"""SEC-01.4 qualification of a registered, completed credential rotation trial.

This reader does not authorize rotation or terminate database sessions. A trusted
host supplies receipts from its one-time, authorized trial and retains the raw
evidence. Neither uploaded JSON nor an AI statement is a receipt provider.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .runner import SHA256, Observation, RunContext, TestSpec, _identifier

SPEC = TestSpec("SEC-01.4", "SEC-01", "1", "success", "required")
RECOVERY_MS = 30_000
MAX_AGE = timedelta(minutes=10)


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware rotation timestamp required")
    return value.astimezone(UTC)


def _digest(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("retained evidence digest required")


def _hash(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class RotationTarget:
    app: str
    environment_ref: str
    workload_uid: str
    database_uid: str
    database_role: str
    # A reference, never the credential or a hash of a low-entropy password.
    secret_ref: str
    revision_sha256: str

    def __post_init__(self):
        for value in (self.app, self.environment_ref, self.workload_uid,
                      self.database_uid, self.database_role, self.secret_ref):
            _identifier(value)
        _digest(self.revision_sha256)


@dataclass(frozen=True)
class ProcessIdentity:
    """Exact serving instance, including an in-container process restart token."""

    pod_uid: str
    container_id: str
    process_started_at: datetime
    restart_count: int

    def __post_init__(self):
        _identifier(self.pod_uid)
        _identifier(self.container_id)
        _time(self.process_started_at)
        if type(self.restart_count) is not int or self.restart_count < 0:
            raise ValueError("registered process identity required")


@dataclass(frozen=True)
class AppDatabaseCheck:
    """One direct-instance HTTP check and independent committed marker readback.

    backend_started_at refers to the database session that committed THIS write,
    not to any healthy connection. A pooler front-end connection alone is not
    sufficient. The host must preserve the instance/backend correlation evidence.
    """

    process: ProcessIdentity
    started_at: datetime
    completed_at: datetime
    health_status: int
    write_status: int
    marker_id: str
    committed_rows: int
    marker_sha256: str
    backend_started_at: datetime | None
    evidence_sha256: str

    def __post_init__(self):
        if (not isinstance(self.process, ProcessIdentity)
                or not _time(self.started_at) <= _time(self.completed_at)
                or any(type(v) is not int or not 100 <= v <= 599
                       for v in (self.health_status, self.write_status))
                or type(self.committed_rows) is not int or self.committed_rows < 0):
            raise ValueError("bound HTTP and committed write evidence required")
        _identifier(self.marker_id)
        _digest(self.marker_sha256)
        _digest(self.evidence_sha256)
        if self.backend_started_at is not None:
            if _time(self.backend_started_at) > self.completed_at:
                raise ValueError("database session started after its observation")
        if self.process.process_started_at > self.started_at:
            raise ValueError("process started after its request")

    @property
    def successful(self):
        return (self.health_status == 200 and self.write_status == 201
                and self.committed_rows == 1 and self.backend_started_at is not None)


@dataclass(frozen=True)
class RotationTrial:
    context: RunContext
    target: RotationTarget
    execution_id: str
    before: AppDatabaseCheck
    requested_at: datetime
    applied_at: datetime
    fenced_at: datetime
    old_version: str
    new_version: str
    # Active version and DB-role authentication confirmed together by the host.
    old_credential_rejected: bool
    new_credential_accepted: bool
    # The exact pre-test sessions were closed; no other application's sessions.
    old_sessions_closed: bool
    after: AppDatabaseCheck
    # One monotonic clock, from before rotation request through committed readback.
    recovery_ms: int
    finished_at: datetime
    trial_evidence_sha256: str

    def __post_init__(self):
        if (not isinstance(self.context, RunContext) or not isinstance(self.target, RotationTarget)
                or not isinstance(self.before, AppDatabaseCheck)
                or not isinstance(self.after, AppDatabaseCheck)
                or type(self.recovery_ms) is not int or self.recovery_ms < 0
                or any(type(v) is not bool for v in (
                    self.old_credential_rejected, self.new_credential_accepted,
                    self.old_sessions_closed))):
            raise ValueError("completed rotation trial required")
        for value in (self.execution_id, self.old_version, self.new_version):
            _identifier(value)
        _digest(self.trial_evidence_sha256)
        if not (self.before.completed_at <= _time(self.requested_at)
                <= _time(self.applied_at) <= _time(self.fenced_at)
                <= self.after.started_at <= self.after.completed_at <= _time(self.finished_at)):
            raise ValueError("rotation stage order invalid")
        elapsed_ms = (self.after.completed_at - self.requested_at).total_seconds() * 1000
        if abs(elapsed_ms - self.recovery_ms) > 1000:
            raise ValueError("wall and monotonic rotation duration disagree")


class SecretRotationProbe:
    """Read-only qualifier; missing or unbound proof is UNKNOWN, never PASS.

    read_current must independently return the current target/process/version.
    It must not echo the receipt. The host owns registration and durable effect
    reconciliation. Re-running this probe must never re-run password rotation.
    """

    def __init__(self, target, *, trial_provider, read_current, evidence_sink,
                 clock=lambda: datetime.now(UTC)):
        if (not isinstance(target, RotationTarget)
                or not all(callable(c) for c in (trial_provider, read_current, evidence_sink, clock))):
            raise ValueError("trusted rotation providers required")
        self.target, self.trials, self.current = target, trial_provider, read_current
        self.sink, self.clock = evidence_sink, clock

    def __call__(self, context, spec):
        if (not isinstance(context, RunContext) or spec != SPEC
                or context.app != self.target.app
                or context.environment.ref != self.target.environment_ref):
            raise ValueError("rotation method or registered target mismatch")
        started = _time(self.clock())
        trial = self.trials(context)
        if (not isinstance(trial, RotationTrial) or trial.context != context
                or trial.target != self.target
                or not trial.finished_at <= started <= trial.finished_at + MAX_AGE
                or trial.requested_at - trial.before.completed_at > timedelta(seconds=5)
                or trial.finished_at - trial.before.started_at > MAX_AGE):
            raise ValueError("current registered trial required")
        current = self.current(context, self.target)
        finished = _time(self.clock())
        # An exact typed tuple is supplied only by the trusted current-state reader.
        if (type(current) is not tuple or len(current) != 4
                or current[0] != self.target or not isinstance(current[1], ProcessIdentity)
                or current[2] != trial.new_version
                or not started <= _time(current[3]) <= finished):
            raise ValueError("current rotation target or version changed")
        before, after = trial.before, trial.after
        positive = before.successful
        reason, observed = None, None
        if not positive:
            reason = "POSITIVE_CONTROL_FAILED"
        elif (trial.old_version == trial.new_version
                or not trial.old_credential_rejected or not trial.new_credential_accepted
                or not trial.old_sessions_closed or before.marker_id == after.marker_id):
            reason = "UNEXPECTED_RESPONSE"
        elif (after.successful and (after.backend_started_at <= trial.applied_at
                                   or after.backend_started_at > after.completed_at)):
            reason = "UNEXPECTED_RESPONSE"
        else:
            # Failure to recover within the budget is FAIL only with completed
            # observation, not a timeout/exception while obtaining evidence.
            unchanged = before.process == after.process == current[1]
            if unchanged and not after.successful and trial.recovery_ms < RECOVERY_MS:
                reason = "UNEXPECTED_RESPONSE"
            else:
                observed = ("success" if unchanged and after.successful
                            and trial.recovery_ms <= RECOVERY_MS else "failure")
        payload = json.dumps({
            "format_version": "1", "test_id": SPEC.test_id, "method_version": "1",
            "run_id": context.run_id, "app": context.app,
            "environment": {"id": context.environment.id, "role": context.environment.role,
                            "ref": context.environment.ref},
            "execution_id": trial.execution_id,
            "target_sha256": _hash(repr(self.target).encode()),
            "trial_evidence_sha256": trial.trial_evidence_sha256,
            "before_evidence_sha256": before.evidence_sha256,
            "after_evidence_sha256": after.evidence_sha256,
            "requested_at": trial.requested_at.isoformat(), "applied_at": trial.applied_at.isoformat(),
            "fenced_at": trial.fenced_at.isoformat(), "finished_at": trial.finished_at.isoformat(),
            "collected_at": finished.isoformat(), "recovery_ms": trial.recovery_ms,
            "recovery_limit_ms": RECOVERY_MS, "positive_control": positive,
            "process_unchanged": before.process == after.process == current[1],
            "observed": observed, "unknown_reason": reason,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "pass" if positive else "fail",
                           finished, "bravo-secret-rotation", "1", _hash(payload), reason)
