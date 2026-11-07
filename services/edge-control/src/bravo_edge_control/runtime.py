"""Single-writer runtime: every interval reads current identity and stored windows."""

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .host import ShiftHost, _key
from .provider import EdgeError


@dataclass(frozen=True)
class RuntimePorts:
    """Deployment-owned authentication and T19 providers, never request payloads."""

    principal: object
    load_session: object
    provider: object = None

    def __post_init__(self):
        if not callable(self.principal) or not callable(self.load_session):
            raise ValueError("current_runtime_ports_required")


class ShiftRuntime:
    def __init__(self, host: ShiftHost, *, principal, plan_id, run_id=None):
        if not isinstance(host, ShiftHost) or not callable(principal):
            raise ValueError("current_shift_host_required")
        self.host, self.principal, self.plan_id = host, principal, plan_id
        self.run_id = run_id

    @contextmanager
    def _owner(self):
        # A watcher owns one LB journal for its whole lifetime, not just a PATCH.
        # The Controller still takes its own per-effect lock inside this lease.
        path = Path(str(self.host.journal.resolve()) + ".runtime.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EdgeError("shift_runtime_already_running") from None
            yield
        finally:
            os.close(fd)

    def command(self, action, operation_id):
        if action not in {"start", "advance", "restore"}:
            raise ValueError("invalid_shift_action")
        with self._owner():
            return getattr(self.host, action)(self.principal(), self.plan_id, operation_id)

    def watch(self, *, stop: Event, interval_seconds=1, emit=lambda _value: None):
        if type(interval_seconds) not in (int, float) or not 0 < interval_seconds <= 5:
            raise ValueError("invalid_observation_interval")
        with self._owner():
            while not stop.is_set():
                # Re-authenticate on each iteration, including after restart.
                result = self.host.observe(self.principal(), self.plan_id)
                emit(result)
                if result["rollback"] is not None:
                    # UNKNOWN/UNCERTAIN is terminal too: never send a second PATCH.
                    return result
                stop.wait(interval_seconds)
        return None

    def sequence(self, *, stop: Event, interval_seconds=1, emit=lambda _value: None, hold_at_full=False):
        """Resume the registered run through 10/50/100/0 and automatic abort.

        hold_at_full (happy path, T49): stop at 100% once its window allows advancing (AT_FULL) instead of
        restoring to 0, so the cutover (operator_ports.edge_at_full) can follow. Default: the drill order.

        Operation IDs derive from the registered run, so a process restart
        cannot create another start/PATCH. Only the canonical T36 decision
        permits advancement. The same process observes throughout the run.
        """
        if type(interval_seconds) not in (int, float) or not 0 < interval_seconds <= 5:
            raise ValueError("invalid_observation_interval")
        if stop.is_set():
            return dict(status="STOPPED", receipt=None)
        with self._owner():
            with self.host.store._transaction(write=True, plan_id=self.plan_id) as c:
                _, current, _ = self.host._current(c, self.principal(), self.plan_id)
                # Detect missing intake schema/read permission before the first PATCH.
                self.host._latest_window(c, self.plan_id)
            if self.run_id is not None and current.run_id != self.run_id:
                raise EdgeError("registered_run_mismatch")
            def operation(weight):
                return _key("runtime-stage", [current.run_id, weight])
            receipt = self.host.start(self.principal(), self.plan_id, operation(10))
            if receipt["status"] == "SUPERSEDED":
                receipt = self.host._controller(lambda _: False).latest_receipt()
            emit(dict(receipt=receipt))
            while not stop.is_set():
                if (receipt["plan_id"] != self.plan_id or receipt["run_id"] != current.run_id
                        or receipt["status"] != "CONFIRMED"):
                    return dict(status="STOPPED", receipt=receipt)
                if receipt["weight_target"] == 0:
                    status = "ABORTED" if receipt["reason"] == "SLI_ABORT" else "RESTORED"
                    return dict(status=status, receipt=receipt)
                result = self.host.observe(self.principal(), self.plan_id)
                emit(result)
                if result["rollback"] is not None:
                    status = "ABORTED" if result["rollback"]["status"] == "CONFIRMED" else "STOPPED"
                    return dict(status=status, **result)
                if result["judgement"]["action"] == "ADVANCE_ALLOWED":
                    if hold_at_full and receipt["weight_target"] == 100:
                        return dict(status="AT_FULL", receipt=receipt)
                    weight = {10: 50, 50: 100, 100: 0}[receipt["weight_target"]]
                    action = self.host.restore if weight == 0 else self.host.advance
                    receipt = action(self.principal(), self.plan_id, operation(weight))
                    emit(dict(receipt=receipt))
                else:
                    stop.wait(interval_seconds)
            return dict(status="STOPPED", receipt=receipt)
