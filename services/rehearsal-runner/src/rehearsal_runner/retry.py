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


class AttemptFactory(Protocol):
    def prepare(self, attempt: int, feedback_ref: str | None) -> AttemptPorts:
        """Prepare a new isolated attempt; must not execute remote effects."""
        ...
