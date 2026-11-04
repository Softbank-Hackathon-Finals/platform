"""T15 single-attempt orchestration boundary; no production adapters are installed.

The returned result is not an approval or an authenticated verification receipt.
The caller must provide T10 admission, durable execution ownership, and validated
T11/T38 adapters before connecting this flow to real resources.
"""

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .isolation.kube import Kubectl
from .isolation.profile import Target
from .isolation_gate import IsolationBlocked, IsolationPass, check_isolation


@dataclass(frozen=True)
class Namespace:
    name: str
    uid: str


@dataclass(frozen=True)
class ProposalRef:
    run_id: str
    source_sha: str
    proposal_digest: str


@dataclass(frozen=True)
class AttemptResult:
    state: Literal["BLOCK", "NEEDS_HUMAN", "RECEIPT_UNVERIFIED"]
    reason: str
    untrusted_receipt: object | None = field(default=None, repr=False, compare=False)
    proposal: ProposalRef | None = field(default=None, repr=False, compare=False)


class Proposer(Protocol):
    def propose(self, task: object) -> ProposalRef:
        """Return a T11-validated proposal reference, not raw model output."""
        ...


class Lifecycle(Protocol):
    def create(self, run_id: str) -> Namespace:
        """Create a fresh namespace and return its server-issued UID."""
        ...

    def cleanup(self, namespace: Namespace) -> bool:
        """Delete by original UID and return True only after confirmed absence."""
        ...


class IsolationGate(Protocol):
    def check(self, namespace: Namespace) -> IsolationPass: ...


@dataclass(frozen=True)
class T3Gate:
    kube: Kubectl
    targets: list[Target]
    image: str

    def check(self, namespace: Namespace) -> IsolationPass:
        return check_isolation(
            self.kube, namespace.name, self.targets, self.image, expected_uid=namespace.uid
        )


class Verifier(Protocol):
    def verify(self, proposal: ProposalRef, namespace: Namespace) -> object:
        """Run to completion against the exact UID; return an untrusted receipt.

        The adapter must enforce UID identity while executing, not just by name.
        Returned receipt still requires T38 authentication and evidence validation.
        """
        ...


class IsolatedVerifier(Protocol):
    def verify_isolated(
        self, proposal: ProposalRef, namespace: Namespace, isolation: IsolationPass
    ) -> object:
        """Use the exact T3 result; return an untrusted receipt."""
        ...


def _valid_proposal(proposal: object, run_id: str) -> bool:
    return (
        isinstance(proposal, ProposalRef)
        and proposal.run_id == run_id
        and isinstance(proposal.source_sha, str)
        and re.fullmatch(r"[a-f0-9]{40}", proposal.source_sha) is not None
        and isinstance(proposal.proposal_digest, str)
        and re.fullmatch(r"[a-f0-9]{64}", proposal.proposal_digest) is not None
    )


def run_once(
    task: object, proposer: Proposer, lifecycle: Lifecycle, isolation: IsolationGate,
    verifier: Verifier | None, *, isolated_verifier: IsolatedVerifier | None = None
) -> AttemptResult:
    """Execute one attempt through injected ports; never emit an approval state."""
    run_id = getattr(task, "run_id", None)
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        return AttemptResult("BLOCK", "INVALID_TASK")
    try:
        proposal = proposer.propose(task)
    except Exception:
        return AttemptResult("NEEDS_HUMAN", "PROPOSAL_UNAVAILABLE")
    if (not _valid_proposal(proposal, run_id)
            or getattr(task, "source_sha", None) != proposal.source_sha):
        return AttemptResult("BLOCK", "PROPOSAL_INVALID")
    if (verifier is None) == (isolated_verifier is None):
        return AttemptResult("BLOCK", "VERIFIER_CONFIGURATION_INVALID")
    # create() may have succeeded before an error: never retry it automatically.
    try:
        namespace = lifecycle.create(run_id)
    except Exception:
        return AttemptResult("NEEDS_HUMAN", "NAMESPACE_CREATE_UNKNOWN")
    if (not isinstance(namespace, Namespace) or not isinstance(namespace.name, str)
            or re.fullmatch(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?", namespace.name) is None
            or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
        return AttemptResult("NEEDS_HUMAN", "NAMESPACE_IDENTITY_UNKNOWN")
    result = AttemptResult("NEEDS_HUMAN", "VERIFICATION_UNAVAILABLE")
    try:
        isolated = isolation.check(namespace)
        if (not isinstance(isolated, IsolationPass) or isolated.namespace != namespace.name
                or isolated.namespace_uid != namespace.uid or not isinstance(isolated.probe_run_id, str)
                or not isolated.probe_run_id.strip()):
            result = AttemptResult("BLOCK", "ISOLATION_IDENTITY_MISMATCH")
        else:
            if isolated_verifier is not None:
                receipt = isolated_verifier.verify_isolated(proposal, namespace, isolated)
            else:
                receipt = verifier.verify(proposal, namespace)
            if receipt is None:
                result = AttemptResult("NEEDS_HUMAN", "RECEIPT_MISSING")
            else:
                # Receipt/evidence authentication and approval binding are a later boundary.
                result = AttemptResult("RECEIPT_UNVERIFIED", "RECEIPT_REQUIRES_VALIDATION", receipt, proposal)
    except IsolationBlocked:
        result = AttemptResult("BLOCK", "ISOLATION_BLOCKED")
    except Exception:
        result = AttemptResult("NEEDS_HUMAN", "EFFECTS_UNKNOWN")
    finally:
        try:
            cleaned = lifecycle.cleanup(namespace) is True
        except Exception:
            cleaned = False
    if not cleaned:
        return AttemptResult("NEEDS_HUMAN", "CLEANUP_UNCONFIRMED")
    return result
