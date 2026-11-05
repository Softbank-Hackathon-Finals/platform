"""T15 bounded retry orchestration with an injected trusted receipt assessor.

The host must provide durable single-execution ownership and an assessor that
independently authenticates T38 evidence. Without them this is a local port,
not a production retry or approval authority.
"""

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .runner import (
    AttemptResult,
    IsolatedVerifier,
    IsolationGate,
    Lifecycle,
    ProposalRef,
    Proposer,
    run_once,
)


@dataclass(frozen=True)
class AttemptPorts:
    proposer: Proposer
    lifecycle: Lifecycle
    isolation: IsolationGate
    verifier: IsolatedVerifier


@dataclass(frozen=True)
class RetryDecision:
    run_id: str
    source_sha: str
    proposal_digest: str
    attempt: int
    outcome: Literal["PASS", "RETRY_ALLOWED", "NEEDS_HUMAN"]
    feedback_ref: str | None = None
    verification_id: str | None = None


class PreviousVerificationHold(Protocol):
    def confirm(self, run_id: str, verification_id: str) -> bool:
        """Confirm an authorized T10 hold was durably committed for this verification."""
        ...


class AttemptFactory(Protocol):
    def prepare(self, attempt: int, feedback_ref: str | None) -> AttemptPorts:
        """Prepare a new isolated attempt; must not execute remote effects."""
        ...


class ReceiptAssessor(Protocol):
    def assess(self, receipt: object, proposal: ProposalRef, attempt: int) -> RetryDecision:
        """Authenticate current evidence, bind its identities, and decide retry eligibility."""
        ...


@dataclass(frozen=True)
class RetryResult:
    state: Literal["NEEDS_HUMAN", "BLOCK", "RECEIPT_UNVERIFIED"]
    reason: str
    attempts: int
    final_attempt: AttemptResult | None = field(default=None, repr=False, compare=False)


def run_retries(
    task: object, factory: AttemptFactory, assessor: ReceiptAssessor, *,
    hold: PreviousVerificationHold | None = None,
) -> RetryResult:
    """Run at most three attempts; never treat a result as approval authority."""
    feedback_ref = None
    for attempt in range(1, 4):
        try:
            ports = factory.prepare(attempt, feedback_ref)
            if (not isinstance(ports, AttemptPorts)
                    or type(getattr(ports.verifier, "attempt", None)) is not int
                    or ports.verifier.attempt != attempt
                    or (hasattr(ports.lifecycle, "attempt")
                        and (type(ports.lifecycle.attempt) is not int or ports.lifecycle.attempt != attempt))):
                raise ValueError("invalid attempt ports")
        except Exception:
            return RetryResult("NEEDS_HUMAN", "ATTEMPT_PREPARATION_UNAVAILABLE", attempt - 1)
        result = run_once(task, ports.proposer, ports.lifecycle, ports.isolation, None,
                          isolated_verifier=ports.verifier)
        if result.state != "RECEIPT_UNVERIFIED":
            return RetryResult(result.state, result.reason, attempt)
        proposal = result.proposal
        if proposal is None or result.untrusted_receipt is None:
            return RetryResult("NEEDS_HUMAN", "RECEIPT_IDENTITY_UNKNOWN", attempt)
        try:
            decision = assessor.assess(result.untrusted_receipt, proposal, attempt)
        except Exception:
            return RetryResult("NEEDS_HUMAN", "RECEIPT_ASSESSMENT_UNAVAILABLE", attempt)
        if (not isinstance(decision, RetryDecision) or decision.run_id != proposal.run_id
                or decision.source_sha != proposal.source_sha
                or decision.proposal_digest != proposal.proposal_digest
                or type(decision.attempt) is not int or decision.attempt != attempt):
            return RetryResult("NEEDS_HUMAN", "RECEIPT_IDENTITY_MISMATCH", attempt)
        if decision.outcome == "PASS":
            return RetryResult("RECEIPT_UNVERIFIED", "APPROVAL_AUTHORITY_REQUIRED", attempt, result)
        if decision.outcome != "RETRY_ALLOWED":
            return RetryResult("NEEDS_HUMAN", "VERIFICATION_REQUIRES_HUMAN", attempt)
        if attempt == 3:
            return RetryResult("NEEDS_HUMAN", "RETRY_LIMIT_REACHED", attempt)
        if (not isinstance(decision.feedback_ref, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", decision.feedback_ref) is None):
            return RetryResult("NEEDS_HUMAN", "RETRY_FEEDBACK_UNAVAILABLE", attempt)
        if (not isinstance(decision.verification_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", decision.verification_id) is None
                or hold is None):
            return RetryResult("NEEDS_HUMAN", "PREVIOUS_HOLD_UNCONFIRMED", attempt)
        try:
            held = hold.confirm(proposal.run_id, decision.verification_id) is True
        except Exception:
            held = False
        if not held:
            return RetryResult("NEEDS_HUMAN", "PREVIOUS_HOLD_UNCONFIRMED", attempt)
        feedback_ref = decision.feedback_ref
    raise AssertionError("unreachable")
