"""T15 adapter for T11 credential migration proposals.

T11 retains all path, source-content and output validation. This adapter never
bypasses the snapshot policy or supplies credentials to the proposal provider.
"""

import asyncio
from dataclasses import dataclass

from .bucket_proposer import Coordinator
from .runner import ProposalRef
from .secret_task import CredentialMigrationTask, prepare_credential_migration

SOURCE_PATHS = ("app/db_secret.py", "app/config.py", "app/main.py")


def credential_summary(task: CredentialMigrationTask) -> str:
    return (
        "Replace direct Secrets Manager credential reads with ESO-injected "
        "/run/bravo/database/credentials.json, preserving cache and authentication-failure refresh. "
        "Edit only app/db_secret.py, app/config.py, and app/main.py. "
        "Fail closed on missing or invalid credentials; never log credentials."
    )


@dataclass(frozen=True)
class T11CredentialProposer:
    coordinator: Coordinator
    provider: object

    def propose(self, task: object) -> ProposalRef:
        if (not isinstance(task, CredentialMigrationTask)
                or task != prepare_credential_migration(task.run_id, task.source_sha)):
            raise ValueError("invalid credential task")
        coordinator = self.coordinator
        bundle = coordinator.bundle
        if (coordinator.state != "READY" or coordinator.attempt != 0
                or getattr(bundle, "run_id", None) != task.run_id
                or getattr(bundle, "source_sha", None) != task.source_sha
                or getattr(bundle, "summary", None) != credential_summary(task)):
            raise ValueError("task and coordinator mismatch")
        # Preserve T11 snapshot checks; a blocked path fails closed here.
        for path in SOURCE_PATHS:
            coordinator.snapshot.read(path)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("synchronous proposer cannot run inside an event loop")
        result = asyncio.run(coordinator.propose(self.provider))
        if (result.get("state") != "PROPOSAL_READY" or type(result.get("attempt")) is not int
                or result["attempt"] != 1 or result.get("run_id") != task.run_id
                or result.get("source_sha") != task.source_sha
                or not result.get("proposal_digest")):
            raise ValueError("T11 proposal unavailable")
        proposal = result.get("proposal")
        edits = proposal.get("edits") if isinstance(proposal, dict) else None
        if not isinstance(edits, list) or not 1 <= len(edits) <= len(SOURCE_PATHS):
            raise ValueError("invalid credential proposal edits")
        paths = [edit.get("path") if isinstance(edit, dict) else None for edit in edits]
        if (any(not isinstance(path, str) or path not in SOURCE_PATHS for path in paths)
                or "app/db_secret.py" not in paths or len(paths) != len(set(paths))):
            raise ValueError("proposal outside credential source scope")
        return ProposalRef(task.run_id, task.source_sha, result["proposal_digest"])
