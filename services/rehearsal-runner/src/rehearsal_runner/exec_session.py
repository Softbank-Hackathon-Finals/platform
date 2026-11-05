"""One rehearsal.exec session per attempt: binding, de-duplication, budget, retries.

The session is opened by the runner after space A passed T3 isolation and the
tool Pod is ready. It answers every request with a contract v1 response and
keeps the exec record in seq order. It never re-executes a request ID, never
re-runs an apply whose effect is unknown, and closes itself on expiry, end of
the model run, a dropped channel or a full record.
"""

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from .exec_contract import (
    SCHEMA_VERSION,
    MalformedRequest,
    Request,
    args_sha256,
    parse_request,
)
from .exec_output import contains_secret, screen


class ActionError(Exception):
    def __init__(self, output: bytes = b""):
        super().__init__(type(self).__name__)
        self.output = output


class NotSent(ActionError):
    """Confirmed not delivered (connection refused, tool Pod not ready); safe to retry."""


class Transient(ActionError):
    """A read that may pass on retry. For apply it means the effect is unknown."""


class ActionDenied(ActionError):
    """The API refused before executing (permission)."""


class ActionFailed(ActionError):
    """The action ran to completion and reported failure."""


class ActionTimeout(ActionError):
    pass


@dataclass(frozen=True)
class Outcome:
    status: dict
    output: bytes = b""
    record: dict = field(default_factory=dict)  # action result keys for the exec record


class Action(Protocol):
    def validate(self, arguments: dict) -> dict:
        """Raise ValueError if invalid; return the record keys for the checked arguments."""
        ...

    def run(self, arguments: dict, deadline: float) -> Outcome: ...


class Journal(Protocol):
    def write(self, seq: int, phase: str, body: dict) -> None:
        """Write the signed `before` or `after` record of one seq exactly once; raise on failure."""
        ...


class JournalUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Space:
    name: str
    uid: str


@dataclass(frozen=True)
class Limits:
    """ADR-0026 estimates; adjust after measuring on the demo target."""

    action_seconds: float = 15
    calls: int = 5
    total_seconds: float = 90
    session_seconds: float = 180
    records: int = 32
    retries: int = 2
    retry_interval: float = 1


@dataclass
class _Entry:
    digest: str
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


class ExecSession:
    def __init__(self, run_id: str, attempt: int, space: Space, actions: dict[str, Action | None], *,
                 secrets: Callable[[], Iterable[str]], space_uid: Callable[[], str | None],
                 limits: Limits | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, journal: Journal | None = None):
        self.run_id, self.attempt, self.space = run_id, attempt, space
        self._actions, self._secrets, self._space_uid = actions, secrets, space_uid
        self.limits, self._clock, self._sleep = limits or Limits(), clock, sleep
        self._lock = threading.Lock()
        self._opened = clock()
        self._first_call: float | None = None
        self._calls = 0
        self._closed = False
        self._effect_unknown = False
        self._entries: dict[str, _Entry] = {}
        self._events: list[dict] = []
        self._journal = journal
        self._journal_failed = False
        self._seq_of: dict[str, int] = {}

    # -- state -------------------------------------------------------------------------------
    def close(self) -> None:
        """End of the model run, a dropped channel or a stopped broker."""
        with self._lock:
            self._closed = True

    @property
    def effect_unknown(self) -> bool:
        return self._effect_unknown

    def events(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._events]

    @property
    def journal_failed(self) -> bool:
        """A before/after record could not be written; the persisted exec record is incomplete."""
        return self._journal_failed

    def _budget(self, now: float) -> dict:
        left = self.limits.total_seconds if self._first_call is None else \
            self.limits.total_seconds - (now - self._first_call)
        return {"calls_left": max(0, min(5, self.limits.calls - self._calls)),
                "seconds_left": max(0, min(90, int(left)))}

    def _response(self, request_id, action, result_code, reason=None, stage=None, status=None,
                  tail=None, truncated=False) -> dict:
        return {"schema_version": SCHEMA_VERSION, "request_id": request_id, "action": action,
                "result_code": result_code, "reason": reason, "stage": stage, "status": status,
                "output_tail": tail, "output_truncated": truncated, "budget": self._budget(self._clock())}

    def _reserve(self, request_id: str, action: str, digest: str, keys: dict) -> int | None:
        """Write the `before` record and return its seq (caller holds the lock); None when full.

        Until `_complete` runs, the event reads as UNKNOWN: a crash between the two
        leaves exactly that in the persisted journal (ADR-0026 4-2).
        """
        if len(self._events) >= self.limits.records:
            self._closed = True
            return None
        seq = len(self._events) + 1
        before = {"run_id": self.run_id, "attempt": self.attempt, "seq": seq, "action": action,
                  "request_id": request_id, "args_sha256": digest} | {k: v for k, v in keys.items() if v is not None}
        if self._journal is not None:
            try:
                self._journal.write(seq, "before", before)
            except Exception:
                self._journal_failed = self._closed = True
                raise JournalUnavailable() from None
        self._events.append(before | {"result_code": "UNKNOWN", "reason": "EFFECT_UNKNOWN", "stage": "EXECUTE"})
        if len(self._events) >= self.limits.records:
            self._closed = True
        return seq

    def _complete(self, seq: int, result: dict) -> None:
        """Write the `after` record (caller holds the lock)."""
        after = {k: v for k, v in result.items() if v is not None}
        event = self._events[seq - 1]
        self._events[seq - 1] = {k: v for k, v in event.items() if k not in ("reason", "stage")} | after
        if self._journal is not None:
            try:
                self._journal.write(seq, "after", after)
            except Exception:
                self._journal_failed = True

    def _record(self, request_id: str, action: str, digest: str, result: dict) -> int | None:
        """A refusal: before and after at once. Returns seq, or None when nothing could be recorded."""
        keys = {k: v for k, v in result.items() if k not in ("result_code", "reason", "stage")}
        outcome = {k: result[k] for k in ("result_code", "reason", "stage") if k in result}
        try:
            seq = self._reserve(request_id, action, digest, keys)
        except JournalUnavailable:
            return None
        if seq is not None:
            self._complete(seq, outcome)
        return seq

    # -- request handling --------------------------------------------------------------------
    def handle(self, raw) -> dict:
        request, request_id, action = parse_request(raw)  # MalformedRequest propagates
        if request is None:
            digest = args_sha256(raw["arguments"]) if isinstance(raw.get("arguments"), dict) else "0" * 64
            return self._refuse(None, request_id, action, digest, "BLOCKED", "ADMIT")
        if request.run_id != self.run_id or request.attempt != self.attempt:
            # Another attempt's request is never written into this attempt's record.
            return self._response(request_id, action, "REJECTED", "FOREIGN_ATTEMPT", "ADMIT")
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                entry = self._entries[request_id] = _Entry(request.args_sha256)
                owner = True
            else:
                owner = False
        if not owner:
            return self._duplicate(request, entry)
        try:
            response = self._admit_and_run(request)
        except Exception:
            response = self._fail_unexpected(request)
        entry.response = response
        entry.done.set()
        return response

    def _duplicate(self, request: Request, entry: _Entry) -> dict:
        if entry.digest != request.args_sha256:
            return self._refuse(request, request.request_id, request.action, request.args_sha256,
                                "BLOCKED", "ADMIT", store=False)
        if not entry.done.wait(self.limits.action_seconds + 1) or entry.response is None:
            return self._response(request.request_id, request.action, "REJECTED", "UNAVAILABLE", "ADMIT")
        return dict(entry.response, budget=self._budget(self._clock()))

    def _refuse(self, request, request_id, action, digest, reason, stage, *, keys=None,
                store=True) -> dict:
        with self._lock:
            if self._closed and reason not in ("EFFECT_UNKNOWN", "BUDGET_EXHAUSTED"):
                pass  # a closed session writes nothing more
            elif self._record(request_id, action, digest,
                              {"result_code": "REJECTED", "reason": reason, "stage": stage} | (keys or {})) is None:
                reason, stage = "UNAVAILABLE", "ADMIT"
        return self._response(request_id, action, "REJECTED", reason, stage)

    def _admit_and_run(self, request: Request) -> dict:
        rid, action, digest = request.request_id, request.action, request.args_sha256
        now = self._clock()
        with self._lock:
            if now - self._opened >= self.limits.session_seconds:
                self._closed = True
            closed, unknown = self._closed, self._effect_unknown
            exhausted = (self._calls >= self.limits.calls or (
                self._first_call is not None and now - self._first_call >= self.limits.total_seconds))
        if closed:
            return self._response(rid, action, "REJECTED", "UNAVAILABLE", "ADMIT")
        if unknown:
            return self._refuse(request, rid, action, digest, "EFFECT_UNKNOWN", "ADMIT")
        if exhausted:
            return self._refuse(request, rid, action, digest, "BUDGET_EXHAUSTED", "ADMIT")
        handler = self._actions.get(action)
        if handler is None or args_sha256(request.arguments) != digest:
            return self._refuse(request, rid, action, digest, "BLOCKED", "ADMIT")
        try:
            keys = handler.validate(request.arguments)
        except Exception:
            return self._refuse(request, rid, action, digest, "BLOCKED", "VALIDATE")
        try:
            leaked = contains_secret(_strings(request.arguments), self._secrets())
        except Exception:
            leaked = True
        if leaked:  # the arguments themselves are not recorded
            return self._refuse(request, rid, action, digest, "BLOCKED", "VALIDATE")
        try:
            same_space = self._space_uid() == self.space.uid
        except Exception:
            same_space = False
        if not same_space:
            return self._refuse(request, rid, action, digest, "BLOCKED", "ADMIT", keys=keys)
        with self._lock:
            try:
                seq = self._reserve(rid, action, digest, keys)
            except JournalUnavailable:
                seq = None
            if seq is None:  # nothing runs without its before record
                return self._response(rid, action, "REJECTED", "UNAVAILABLE", "ADMIT")
            self._seq_of[rid] = seq
            start = self._clock()
            if self._first_call is None:
                self._first_call = start
            self._calls += 1
            left = self.limits.total_seconds - (start - self._first_call)
        deadline = start + max(0.0, min(self.limits.action_seconds, left))
        result, status, output = self._execute(handler, action, request.arguments, deadline)
        return self._finish(request, seq, result, status, output)

    def _execute(self, handler: Action, action: str, arguments: dict, deadline: float):
        tries = 0
        while True:
            try:
                outcome = handler.run(arguments, deadline)
                return {"result_code": "OK"} | outcome.record, outcome.status, outcome.output
            except NotSent as error:
                failure = ({"result_code": "FAILED", "reason": "UNAVAILABLE", "stage": "EXECUTE"}, error.output)
            except Transient as error:
                if action == "apply":
                    return self._unknown(), None, b""
                failure = ({"result_code": "FAILED", "reason": "UNAVAILABLE", "stage": "EXECUTE"}, error.output)
            except ActionTimeout:
                if action == "apply":
                    return self._unknown(), None, b""
                return {"result_code": "FAILED", "reason": "TIMEOUT", "stage": "EXECUTE"}, None, b""
            except ActionDenied:
                return {"result_code": "REJECTED", "reason": "BLOCKED", "stage": "EXECUTE"}, None, b""
            except ActionFailed as error:
                return {"result_code": "FAILED", "reason": "NONZERO_EXIT", "stage": "EXECUTE"}, None, error.output
            except Exception:
                if action == "apply":
                    return self._unknown(), None, b""
                return {"result_code": "FAILED", "reason": "UNAVAILABLE", "stage": "EXECUTE"}, None, b""
            tries += 1
            if tries > self.limits.retries or deadline - self._clock() <= self.limits.retry_interval:
                return failure[0], None, failure[1]
            self._sleep(self.limits.retry_interval)

    def _unknown(self) -> dict:
        with self._lock:
            self._effect_unknown = True
        return {"result_code": "UNKNOWN", "reason": "EFFECT_UNKNOWN", "stage": "EXECUTE"}

    def _finish(self, request: Request, seq: int, result: dict, status, output: bytes) -> dict:
        screened = screen(output, self._secrets)
        if screened.withheld and result["result_code"] == "OK":
            result = result | {"reason": "OUTPUT_WITHHELD", "stage": "OUTPUT"}
        result = result | {"output_sha256": screened.sha256, "output_bytes": screened.size}
        with self._lock:
            self._complete(seq, result)
        ok = result["result_code"] == "OK"
        return self._response(request.request_id, request.action, result["result_code"],
                              result.get("reason"), result.get("stage"), status if ok else None,
                              None if screened.withheld or result["result_code"] in ("REJECTED", "UNKNOWN")
                              else screened.tail,
                              False if screened.withheld else screened.truncated)

    def _fail_unexpected(self, request: Request) -> dict:
        seq = self._seq_of.get(request.request_id)
        if seq is None:
            return self._response(request.request_id, request.action, "REJECTED", "UNAVAILABLE", "ADMIT")
        result = self._unknown() if request.action == "apply" else \
            {"result_code": "FAILED", "reason": "UNAVAILABLE", "stage": "EXECUTE"}
        with self._lock:
            self._complete(seq, result)
        return self._response(request.request_id, request.action, result["result_code"], result["reason"],
                              "EXECUTE")


def _strings(value) -> str:
    """All string content of the arguments, for the rehearsal secret check."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_strings(k) + "\n" + _strings(v) for k, v in value.items())
    if isinstance(value, list):
        return "\n".join(_strings(v) for v in value)
    return ""


__all__ = ["Action", "ActionDenied", "ActionError", "ActionFailed", "ActionTimeout", "ExecSession",
           "Journal", "JournalUnavailable", "Limits", "MalformedRequest", "NotSent", "Outcome", "Space", "Transient"]
