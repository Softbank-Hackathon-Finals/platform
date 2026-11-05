"""Attempt order when rehearsal.exec is on: AI works in space A, grading in space B (ADR-0025).

    space A create -> T3 isolation -> tool Pod + exec session -> model proposal
    -> close session -> delete A by UID and confirm absence -> run_once (space B)

Space B is the unchanged `run_once` flow fed with the proposal the model already
submitted, so nothing the model did in space A reaches grading. If space A's
absence is not confirmed, space B is never created. With exec off (the
default) the caller uses `run_once` directly and this module is not involved.
"""

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from .exec_actions import Inspector, KubectlRunner, Prober, tool_pod_manifest
from .exec_contract import MalformedRequest, parse_request
from .exec_session import Action, ExecSession, Limits, Space
from .isolation.kube import Kubectl
from .isolation_gate import IsolationBlocked, IsolationPass
from .runner import (
    AttemptResult,
    IsolatedVerifier,
    IsolationGate,
    Lifecycle,
    Namespace,
    ProposalRef,
    run_once,
)

_NAMESPACE = re.compile(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?")


@dataclass(frozen=True)
class ExecConfig:
    """Which task kinds may use exec. Empty until the happy path is verified."""

    enabled_kinds: frozenset[str] = frozenset()

    def enabled(self, kind: str) -> bool:
        return kind in self.enabled_kinds


@dataclass
class ExecTrace:
    """What happened in space A, for the attempt record (persisted by the caller)."""

    space: Namespace | None = None
    removed: bool = False
    opened: bool = False
    effect_unknown: bool = False
    events: list[dict] = field(default_factory=list)


class ExecPhase(Protocol):
    lifecycle: Lifecycle  # space A only; separate from the grading lifecycle
    isolation: IsolationGate
    traces: list[ExecTrace]

    def open(self, run_id: str, attempt: int, namespace: Namespace) -> ExecSession:
        """Prepare the tool Pod and register a session for this attempt's space A."""
        ...

    def close(self, session: ExecSession) -> None: ...


class ExecRegistry:
    """Open sessions by (run_id, attempt); the T15 path hands every channel request here."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, int], ExecSession] = {}

    def register(self, session: ExecSession) -> None:
        with self._lock:
            key = (session.run_id, session.attempt)
            if key in self._sessions:
                raise ValueError("exec session already open for this attempt")
            self._sessions[key] = session

    def unregister(self, session: ExecSession) -> None:
        session.close()
        with self._lock:
            if self._sessions.get((session.run_id, session.attempt)) is session:
                del self._sessions[(session.run_id, session.attempt)]

    def handle(self, raw) -> dict:
        request, request_id, action = parse_request(raw)  # MalformedRequest propagates
        with self._lock:
            session = self._sessions.get((raw.get("run_id"), raw.get("attempt"))) if request else None
            other = any(run == raw.get("run_id") for run, _ in self._sessions)
        if session is not None:
            return session.handle(raw)
        reason = "FOREIGN_ATTEMPT" if other else "UNAVAILABLE"
        budget = {"calls_left": 0, "seconds_left": 0}
        return {"schema_version": 1, "request_id": request_id, "action": action, "result_code": "REJECTED",
                "reason": reason if request else "BLOCKED", "stage": "ADMIT", "status": None,
                "output_tail": None, "output_truncated": False, "budget": budget}


@dataclass
class ToolPodExecPhase:
    """Production `ExecPhase`: runner-template tool Pod and the inspect/probe actions."""

    lifecycle: Lifecycle
    isolation: IsolationGate
    kubectl: Kubectl = field(repr=False)
    registry: ExecRegistry = field(repr=False)
    tool_image: str
    secrets: Callable[[Namespace], Iterable[str]] = field(repr=False)  # values the runner put in A
    applier: Callable[[Space], Action] | None = field(default=None, repr=False)  # per task kind
    log_container: str | None = None
    limits: Limits | None = None
    ready_seconds: int = 60
    traces: list[ExecTrace] = field(default_factory=list, repr=False)

    def open(self, run_id: str, attempt: int, namespace: Namespace) -> ExecSession:
        space = Space(namespace.name, namespace.uid)
        self.kubectl.create(tool_pod_manifest(space, self.tool_image, run_id, attempt))
        if not self.kubectl.wait_ready(space.name, "rehearsal-exec-tool", self.ready_seconds):
            raise RuntimeError("tool Pod not ready")
        runner = KubectlRunner(self.kubectl.context)
        actions = {"inspect": Inspector(space, runner, self.log_container), "probe": Prober(space, runner),
                   "apply": self.applier(space) if self.applier else None}
        session = ExecSession(run_id, attempt, space, actions, secrets=lambda: self.secrets(namespace),
                              space_uid=lambda: _live_uid(self.kubectl, space.name), limits=self.limits)
        self.registry.register(session)
        return session

    def close(self, session: ExecSession) -> None:
        self.registry.unregister(session)


def _live_uid(kubectl: Kubectl, name: str) -> str | None:
    meta = kubectl.get_json("namespace", name).get("metadata") or {}
    return None if meta.get("deletionTimestamp") else meta.get("uid")


@dataclass(frozen=True)
class _Submitted:
    """Hands run_once the proposal the model already submitted; never calls the model again."""

    proposal: ProposalRef

    def propose(self, task: object) -> ProposalRef:
        return self.proposal


def run_with_exec(task: object, proposer, phase: ExecPhase, lifecycle: Lifecycle, isolation: IsolationGate,
                  verifier: IsolatedVerifier, attempt: int) -> AttemptResult:
    """One attempt with exec. The trace of space A is appended to `phase.traces`."""
    run_id = getattr(task, "run_id", None)
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        return AttemptResult("BLOCK", "INVALID_TASK")
    trace = ExecTrace()
    phase.traces.append(trace)
    try:
        space = phase.lifecycle.create(run_id)
    except Exception:
        return AttemptResult("NEEDS_HUMAN", "EXEC_SPACE_CREATE_UNKNOWN")
    if (not isinstance(space, Namespace) or not isinstance(space.name, str)
            or _NAMESPACE.fullmatch(space.name) is None or not isinstance(space.uid, str)
            or not space.uid.strip()):
        return AttemptResult("NEEDS_HUMAN", "EXEC_SPACE_IDENTITY_UNKNOWN")
    trace.space = space
    proposal, failure, session = None, None, None
    try:
        isolated = phase.isolation.check(space)
        if (not isinstance(isolated, IsolationPass) or isolated.namespace != space.name
                or isolated.namespace_uid != space.uid):
            failure = AttemptResult("BLOCK", "ISOLATION_IDENTITY_MISMATCH")
        else:
            try:
                session = phase.open(run_id, attempt, space)
                trace.opened = True
            except Exception:
                session = None  # the model still proposes; its exec calls are refused as UNAVAILABLE
            try:
                proposal = proposer.propose(task)
            except Exception:
                failure = AttemptResult("NEEDS_HUMAN", "PROPOSAL_UNAVAILABLE")
    except IsolationBlocked:
        failure = AttemptResult("BLOCK", "ISOLATION_BLOCKED")
    except Exception:
        failure = AttemptResult("NEEDS_HUMAN", "EXEC_SPACE_UNAVAILABLE")
    finally:
        if session is not None:
            try:
                phase.close(session)
            except Exception:
                pass
            trace.events, trace.effect_unknown = session.events(), session.effect_unknown
        try:
            trace.removed = phase.lifecycle.cleanup(space) is True
        except Exception:
            trace.removed = False
    if not trace.removed:
        return AttemptResult("NEEDS_HUMAN", "EXEC_SPACE_CLEANUP_UNCONFIRMED")
    if failure is not None:
        return failure
    return run_once(task, _Submitted(proposal), lifecycle, isolation, None, isolated_verifier=verifier)


__all__ = ["ExecConfig", "ExecPhase", "ExecRegistry", "ExecTrace", "MalformedRequest", "ToolPodExecPhase",
           "run_with_exec"]
