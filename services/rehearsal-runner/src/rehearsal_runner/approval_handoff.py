"""Prepare T15 candidate data for T10; never create an approval or send it.

These values are untrusted until the T10 receiver authenticates the T15 sender,
resolves the verifier's original evidence, and checks current artifacts, cleanup,
cancellation and holds. The wire schema and transport remain subject to agreement.
"""

import re
from dataclasses import dataclass, field

from .integration_handoff import ProposedHandoff
from .runner import Namespace, ProposalRef

_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


@dataclass(frozen=True)
class CandidateSubmission:
    plan_id: str
    run_id: str
    attempt: int
    verification_id: str
    evidence_ref: str
    proposal_digest: str
    diff_hash: str
    image_digest: str
    target_environment_id: str
    namespace: str
    namespace_uid: str
    cleanup_uid: str

    def t10_fields(self) -> dict:
        """Return the T10 intake fields, never the locally asserted UID evidence.

        T10's authenticated original supplies created/removed UID and absence.
        The host must send these fields only via a configured trusted transport.
        """
        return {
            "plan_id": self.plan_id, "run_id": self.run_id, "attempt": self.attempt,
            "verification_id": self.verification_id, "evidence_ref": self.evidence_ref,
            "target_environment_id": self.target_environment_id, "namespace": self.namespace,
            "proposal_digest": self.proposal_digest, "diff_hash": self.diff_hash,
            "image_digest": self.image_digest,
        }


def prepare_candidate(
    handoff: ProposedHandoff,
    *,
    plan_id: str,
    verification_id: str,
    evidence_ref: str,
    cleanup_uid: str,
    cleanup_confirmed: bool,
) -> CandidateSubmission:
    """Create candidate data only; the receiver must verify every assertion."""
    if not isinstance(handoff, ProposedHandoff):
        raise ValueError("invalid handoff")
    if (not isinstance(handoff.proposal, ProposalRef) or not isinstance(handoff.namespace, Namespace)
            or type(handoff.attempt) is not int or not 1 <= handoff.attempt <= 3
            or not isinstance(handoff.proposal.source_sha, str)
            or re.fullmatch(r"[a-f0-9]{40}", handoff.proposal.source_sha) is None
            or not isinstance(handoff.proposal.proposal_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", handoff.proposal.proposal_digest) is None
            or any(not isinstance(value, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None
                   for value in (handoff.diff_hash, handoff.image_digest))):
        raise ValueError("invalid handoff binding")
    for value in (plan_id, verification_id, evidence_ref, handoff.proposal.run_id,
                  handoff.target_environment_id, handoff.namespace.name):
        if not isinstance(value, str) or re.fullmatch(_IDENTIFIER, value) is None:
            raise ValueError("invalid reference")
    if (cleanup_confirmed is not True or not isinstance(cleanup_uid, str)
            or not cleanup_uid.strip() or cleanup_uid != handoff.namespace.uid):
        raise ValueError("cleanup identity not confirmed")
    # This object is deliberately not CurrentChange and provides no transport.
    return CandidateSubmission(
        plan_id=plan_id, run_id=handoff.proposal.run_id, attempt=handoff.attempt,
        verification_id=verification_id, evidence_ref=evidence_ref,
        proposal_digest=handoff.proposal.proposal_digest, diff_hash=handoff.diff_hash,
        image_digest=handoff.image_digest, target_environment_id=handoff.target_environment_id,
        namespace=handoff.namespace.name, namespace_uid=handoff.namespace.uid, cleanup_uid=cleanup_uid,
    )


@dataclass(frozen=True)
class IntakeResult:
    """Intake acknowledgement only; not human approval or deployment authority."""

    state: str
    verification_id: str
    attempt: int
    revision: str | None = None


def submit_candidate(submission: CandidateSubmission, *, candidate_type, intake, token_supplier) -> IntakeResult:
    """Call a host-configured T10 intake once, without logging token or evidence.

    T10 must authenticate the workload, independently read the verifier's
    original and confirm the artifacts, cleanup, current plan and holds. This
    function does not authenticate evidence, grant approval, retry, or install a
    transport. An exception or malformed acknowledgement leaves the remote
    outcome unknown; the host must resolve it by the T10 idempotency contract.
    """
    if not isinstance(submission, CandidateSubmission):
        raise ValueError("invalid submission")
    unknown = IntakeResult("UNKNOWN", submission.verification_id, submission.attempt)
    try:
        candidate = candidate_type(**submission.t10_fields())
        candidate.validate()
        token = token_supplier()
        if not isinstance(token, str) or not token:
            return unknown
        receipt = intake.receive(token, candidate)
        if (not isinstance(receipt, dict)
                or set(receipt) != {"verification_id", "attempt", "revision"}
                or receipt["verification_id"] != submission.verification_id
                or type(receipt["attempt"]) is not int or receipt["attempt"] != submission.attempt
                or not isinstance(receipt["revision"], str)
                or re.fullmatch(r"[a-f0-9]{64}", receipt["revision"]) is None):
            return unknown
        return IntakeResult("ACKNOWLEDGED", submission.verification_id, submission.attempt, receipt["revision"])
    except Exception:
        return unknown


@dataclass(frozen=True)
class T10PreviousHold:
    """Host-provided authorized hold port used before a subsequent attempt.

    The credential and authorization policy belong to T10's management host;
    this object does not make the T15 workload a hold authority.
    """

    plan_id: str
    run_id: str
    decision_ref: str
    intake: object = field(repr=False)
    credential_supplier: object = field(repr=False)

    def confirm(self, run_id: str, verification_id: str) -> bool:
        if run_id != self.run_id or any(
            not isinstance(value, str) or re.fullmatch(_IDENTIFIER, value) is None
            for value in (self.plan_id, self.run_id, self.decision_ref, verification_id)
        ):
            return False
        try:
            receipt = self.intake.hold(self.credential_supplier(), self.plan_id, verification_id,
                                       decision_ref=self.decision_ref)
            return (isinstance(receipt, dict) and set(receipt) == {"verification_id", "revision"}
                    and receipt["verification_id"] == verification_id
                    and isinstance(receipt["revision"], str)
                    and re.fullmatch(r"held-[a-f0-9]{64}", receipt["revision"]) is not None)
        except Exception:
            return False
