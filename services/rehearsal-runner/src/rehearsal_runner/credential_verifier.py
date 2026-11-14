"""In-process credential task handoff; no production verifier is installed."""

import re
from dataclasses import dataclass
from typing import Protocol

from .runner import Namespace, ProposalRef
from .secret_task import CredentialMigrationTask, same_task


@dataclass(frozen=True)
class CredentialVerificationRequest:
    """Local handoff only, not a T38 wire format or an authorization."""

    run_id: str
    source_sha: str
    proposal_digest: str
    namespace: Namespace
    attempt: int = 1


class CredentialVerificationJob(Protocol):
    def verify(self, request: CredentialVerificationRequest) -> object:
        """Use independent fixtures and enforce the namespace UID throughout."""
        ...


@dataclass(frozen=True)
class CredentialVerifier:
    task: CredentialMigrationTask
    job: CredentialVerificationJob
    attempt: int = 1

    def verify(self, proposal: ProposalRef, namespace: Namespace) -> object:
        task = self.task
        if type(self.attempt) is not int or not 1 <= self.attempt <= 3:
            raise ValueError("invalid verification attempt")
        if (not isinstance(task, CredentialMigrationTask)
                or task != same_task(task)):
            raise ValueError("invalid credential migration task")
        if (not isinstance(proposal, ProposalRef) or proposal.run_id != task.run_id
                or proposal.source_sha != task.source_sha
                or not isinstance(proposal.proposal_digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", proposal.proposal_digest) is None):
            raise ValueError("proposal does not match credential task")
        if (not isinstance(namespace, Namespace) or not isinstance(namespace.name, str)
                or re.fullmatch(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?", namespace.name) is None
                or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
            raise ValueError("invalid namespace identity")
        return self.job.verify(CredentialVerificationRequest(
            run_id=task.run_id, source_sha=task.source_sha,
            proposal_digest=proposal.proposal_digest, namespace=namespace, attempt=self.attempt,
        ))
