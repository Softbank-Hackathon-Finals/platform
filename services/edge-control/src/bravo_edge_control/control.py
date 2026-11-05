"""One durable journal and one writer per LB; no remote exactly-once claim."""

import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from bravo_observability.contract import validate

from .provider import Cloudflare, EdgeError


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Command:
    plan_id: str
    run_id: str
    operation_id: str
    expected_revision: int
    expected_weight: int
    weight_target: int
    reason: str = "SHIFT"

    def __post_init__(self):
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in (
            self.plan_id, self.run_id, self.operation_id
        )):
            raise ValueError("invalid_command_identity")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("invalid_revision")
        if any(type(v) is not int or v not in (0, 10, 50, 100)
               for v in (self.expected_weight, self.weight_target)):
            raise ValueError("unsupported_weight")
        if self.reason not in ("SHIFT", "SLI_ABORT", "FAILBACK"):
            raise ValueError("invalid_reason")
        if self.reason != "SHIFT" and self.weight_target != 0:
            raise ValueError("restore_requires_zero")
        if self.weight_target != 0 and (self.expected_weight, self.weight_target) not in (
            (0, 10), (10, 50), (50, 100)
        ):
            raise ValueError("invalid_shift_step")


class Controller:
    def __init__(self, provider: Cloudflare, journal: Path, *,
                 authorize: Callable[[Command], bool], clock: Callable[[], str] = _now):
        self.provider = provider
        self.path = journal.resolve()
        self.authorize = authorize
        self.clock = clock
        self.binding = _json(asdict(provider.binding))
        with self._locked() as db:
            db.execute("CREATE TABLE IF NOT EXISTS binding (value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM binding").fetchone()
            if row is None:
                db.execute("INSERT INTO binding VALUES (?)", (self.binding,))
            elif row[0] != self.binding:
                raise EdgeError("journal_binding_mismatch")
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                revision INTEGER PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                command TEXT NOT NULL, receipt TEXT NOT NULL)""")
            db.commit()

    @contextmanager
    def _locked(self):
        # Hold the OS lock across network calls, but commit intent before any PATCH.
        # All processes must share this persistent local volume (not separate replicas).
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db_fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(db_fd)
            db = sqlite3.connect(self.path)
            try:
                db.execute("PRAGMA synchronous=FULL")
                yield db
            finally:
                db.close()

    @staticmethod
    def _save(db, receipt):
        db.execute("UPDATE operations SET receipt=? WHERE revision=?",
                   (_json(receipt), receipt["revision"]))
        db.commit()

    def execute(self, command: Command) -> dict:
        with self._locked() as db:
            if self.authorize(command) is not True:
                raise EdgeError("current_authority_required")
            last = db.execute("SELECT revision, receipt FROM operations ORDER BY revision DESC LIMIT 1").fetchone()
            revision = last[0] if last else 0
            old = db.execute("SELECT command, receipt FROM operations WHERE operation_id=?",
                             (command.operation_id,)).fetchone()
            if old:
                if old[0] != _json(asdict(command)):
                    raise EdgeError("idempotency_conflict")
                receipt = json.loads(old[1])
                if receipt["revision"] != revision:
                    return {**receipt, "status": "SUPERSEDED"}
                return self._readback(db, receipt)
            if revision != command.expected_revision:
                raise EdgeError("stale_revision")
            if last and json.loads(last[1])["status"] != "CONFIRMED":
                raise EdgeError("pending_operation_requires_resolution")
            # A completed/aborted run cannot be restarted by a new operation key.
            for (encoded,) in db.execute("SELECT command FROM operations"):
                prior = json.loads(encoded)
                if prior["run_id"] == command.run_id and (
                    prior["plan_id"] != command.plan_id or
                    prior["weight_target"] == 0 and command.weight_target != 0
                ):
                    raise EdgeError("run_closed_or_rebound")
            if self.provider.read() != command.expected_weight:
                raise EdgeError("current_weight_mismatch")
            receipt = {"revision": revision + 1, **asdict(command),
                       "lb_ref": self.provider.binding.lb_id,
                       "request_at": self.clock(), "response_at": None,
                       "confirmed_at": None, "readback_at": None,
                       "status": "UNCERTAIN", "error": None, "readback_weights": None}
            db.execute("INSERT INTO operations VALUES (?,?,?,?)",
                       (revision + 1, command.operation_id, _json(asdict(command)), _json(receipt)))
            db.commit()
            try:
                self.provider.set_weight(command.weight_target)
            except EdgeError as exc:
                # A later GET cannot prove a timed-out PATCH has finished. Never resend.
                receipt["error"] = str(exc)
                receipt["response_at"] = self.clock()
                self._save(db, receipt)
                return self._readback(db, receipt)
            receipt["response_at"] = self.clock()
            receipt["status"] = "AWAITING_READBACK"
            self._save(db, receipt)
            return self._readback(db, receipt)

    def _readback(self, db, receipt):
        try:
            observed = self.provider.read()
            receipt["readback_at"] = self.clock()
            receipt["readback_weights"] = {"source": 100 - observed, "target": observed}
            if receipt["status"] != "UNCERTAIN":
                receipt["status"] = ("CONFIRMED" if observed == receipt["weight_target"]
                                     else "AWAITING_READBACK")
                if receipt["status"] == "CONFIRMED":
                    receipt["confirmed_at"] = receipt["confirmed_at"] or self.clock()
                receipt["error"] = None if receipt["status"] == "CONFIRMED" else "readback_mismatch"
        except EdgeError as exc:
            if receipt["status"] != "UNCERTAIN":
                receipt["status"] = "AWAITING_READBACK"
            receipt["error"] = str(exc)
        self._save(db, receipt)
        return receipt


def sli_rollback(judgement: dict, *, plan_id: str, run_id: str, revision: int,
                 weight: int, observe_from: str) -> Command | None:
    """Consume T36's stored decision, never reimplement its thresholds.

    All keyword arguments and judgement must come from T10's current authority,
    under its plan lock. Untrusted window payloads are not decision authority.
    Controller.authorize must recheck that authority immediately before dispatch.
    """
    validate("SliJudgement", judgement)
    if (judgement["plan_id"], judgement["weight_target"], judgement["observe_from"]) != (
        plan_id, weight, observe_from
    ):
        raise EdgeError("stale_judgement")
    if judgement["state"] != "SHIFT" or judgement["action"] != "SET_WEIGHT_0":
        return None
    if judgement["verdict"] not in ("FAIL", "UNKNOWN"):
        raise EdgeError("inconsistent_judgement")
    digest = hashlib.sha256(_json(judgement).encode()).hexdigest()
    return Command(plan_id, run_id, "sli-" + digest, revision, weight, 0, "SLI_ABORT")


def apply_sli(controller: Controller, judgement: dict, **current) -> dict | None:
    """Dispatch the canonical SHIFT abort immediately; PASS/HOLD/WATCH do no writes."""
    command = sli_rollback(judgement, **current)
    return controller.execute(command) if command is not None else None


def failback_operation_id(*, plan_id: str, command_id: str, execution_id: str,
                          epoch: int) -> str:
    if type(epoch) is not int or epoch < 1 or any(
        not isinstance(value, str) or not value
        for value in (plan_id, command_id, execution_id)
    ):
        raise ValueError("invalid_failback_identity")
    return "failback-" + hashlib.sha256(
        _json([plan_id, command_id, execution_id, epoch]).encode()
    ).hexdigest()


class FailbackEdge:
    """Implements T10 EdgePort.restore_origin; resolve from its durable command record."""

    def __init__(self, controller: Controller, resolve: Callable[..., Command]):
        self.controller = controller
        self.resolve = resolve

    def restore_origin(self, *, plan_id: str, command_id: str, execution_id: str,
                       epoch: int) -> bool:
        operation_id = failback_operation_id(plan_id=plan_id, command_id=command_id,
                                             execution_id=execution_id, epoch=epoch)
        command = self.resolve(plan_id=plan_id, command_id=command_id,
                               execution_id=execution_id, epoch=epoch)
        if (command.plan_id != plan_id or command.reason != "FAILBACK" or
                command.weight_target != 0 or command.operation_id != operation_id):
            raise EdgeError("failback_binding_mismatch")
        return self.controller.execute(command)["status"] == "CONFIRMED"
