"""Prepare T15 candidate data for T10; never create an approval or send it.

These values are untrusted until the T10 receiver authenticates the T15 sender,
resolves the verifier's original evidence, and checks current artifacts, cleanup,
cancellation and holds. The wire schema and transport remain subject to agreement.
"""

import re
from dataclasses import dataclass

from .integration_handoff import ProposedHandoff

_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"


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
    for value in (plan_id, verification_id, evidence_ref):
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
