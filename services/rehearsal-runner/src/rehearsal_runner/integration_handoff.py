"""Local T15 handoff proposal; this is not a T38 wire contract or T10 authority.

No result from this module authenticates a Job, verifies an artifact, authorizes a
write, or turns an untrusted receipt into a CurrentChange. Those require the
producer and T10 contracts to be connected by their trusted hosts.
"""

import hashlib
import re
from dataclasses import dataclass

from .isolation_gate import IsolationPass
from .runner import Namespace, ProposalRef, _valid_proposal


@dataclass(frozen=True)
class ProposedHandoff:
    """Candidate values for an integration discussion and local simulation."""

    proposal: ProposalRef
    namespace: Namespace
    isolation_probe_run_id: str
    attempt: int
    diff_hash: str
    image_digest: str
    target_environment_id: str


def prepare_handoff(
    proposal: ProposalRef,
    namespace: Namespace,
    isolation: IsolationPass,
    *,
    attempt: int,
    diff: bytes,
    image_digest: str,
    target_environment_id: str,
) -> ProposedHandoff:
    """Bind local input values; the receiving host must independently verify them.

    ``diff`` is the exact candidate byte sequence; callers must separately prove
    that it and the image were produced from the referenced proposal. This method
    is deliberately not a verified evidence producer or a retry decision.
    """
    if (not isinstance(proposal, ProposalRef) or not isinstance(proposal.run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", proposal.run_id) is None
            or not _valid_proposal(proposal, proposal.run_id)):
        raise ValueError("invalid proposal")
    if (not isinstance(namespace, Namespace) or not isinstance(namespace.name, str)
            or re.fullmatch(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?", namespace.name) is None
            or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
        raise ValueError("invalid namespace")
    if (not isinstance(isolation, IsolationPass) or isolation.namespace != namespace.name
            or isolation.namespace_uid != namespace.uid
            or not isinstance(isolation.probe_run_id, str) or not isolation.probe_run_id.strip()):
        raise ValueError("isolation identity mismatch")
    if type(attempt) is not int or attempt not in (1, 2, 3):
        raise ValueError("invalid attempt")
    if not isinstance(diff, bytes) or not diff:
        raise ValueError("candidate diff is required")
    if not isinstance(image_digest, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest) is None:
        raise ValueError("invalid image digest")
    if (not isinstance(target_environment_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", target_environment_id) is None):
        raise ValueError("invalid target environment")
    return ProposedHandoff(
        proposal=proposal, namespace=namespace, isolation_probe_run_id=isolation.probe_run_id,
        attempt=attempt, diff_hash="sha256:" + hashlib.sha256(diff).hexdigest(),
        image_digest=image_digest, target_environment_id=target_environment_id,
    )
