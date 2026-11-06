"""Ports required to prepare a single admitted rehearsal attempt."""

from dataclasses import dataclass
from typing import Protocol

from .runner import IsolatedVerifier, IsolationGate, Lifecycle, Proposer


@dataclass(frozen=True)
class AttemptPorts:
    proposer: Proposer
    lifecycle: Lifecycle
    isolation: IsolationGate
    verifier: IsolatedVerifier
    exec_phase: object | None = None  # exec_flow.ExecPhase; None (default) keeps the run_once order


class AttemptFactory(Protocol):
    def prepare(self, attempt: int, feedback_ref: str | None) -> AttemptPorts:
        """Prepare a new isolated attempt; must not execute remote effects."""
        ...
