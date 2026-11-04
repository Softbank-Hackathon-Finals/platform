"""Local bucket-verification bridge; observations are not T38 receipts.

The job port is injected and has no production implementation here. The caller
must provide an isolated verifier that enforces the namespace UID throughout
execution and obtains its own credentials and fixture, independently of T11.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from .bucket_task import BucketPolicyTask, prepare_bucket_policy_task
from .runner import Namespace, ProposalRef


@dataclass(frozen=True)
class BucketVerificationRequest:
    """In-process handoff, not a T38 wire format or authorization."""

    run_id: str
    source_sha: str
    source_policy_sha256: str
    proposal_digest: str
    target_bucket: str
    namespace: Namespace


class BucketVerificationJob(Protocol):
    def verify(self, request: BucketVerificationRequest) -> object:
        """Return untrusted observations after enforcing namespace identity."""
        ...


@dataclass(frozen=True)
class BucketVerifier:
    task: BucketPolicyTask
    job: BucketVerificationJob

    def verify(self, proposal: ProposalRef, namespace: Namespace) -> object:
        task = self.task
        if not isinstance(task, BucketPolicyTask):
            raise ValueError("bucket task required")
        if task != prepare_bucket_policy_task(
            task.run_id, task.source_policy, task.target_bucket, source_sha=task.source_sha
        ):
            raise ValueError("invalid bucket task")
        if (not isinstance(proposal, ProposalRef) or proposal.run_id != task.run_id
                or proposal.source_sha != task.source_sha
                or not isinstance(proposal.proposal_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", proposal.proposal_digest) is None):
            raise ValueError("proposal does not match bucket task")
        if (not isinstance(namespace, Namespace) or not isinstance(namespace.name, str)
                or re.fullmatch(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?", namespace.name) is None
                or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
            raise ValueError("invalid namespace identity")
        return self.job.verify(BucketVerificationRequest(
            run_id=task.run_id,
            source_sha=task.source_sha,
            source_policy_sha256=task.source_sha256,
            proposal_digest=proposal.proposal_digest,
            target_bucket=task.target_bucket,
            namespace=namespace,
        ))
