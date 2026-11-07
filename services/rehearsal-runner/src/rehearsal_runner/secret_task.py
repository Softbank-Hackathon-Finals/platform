"""Prepare metadata for a demo-chat credentials migration proposal.

The source kind (ADR-0034) names the secret store the app reads directly today; the target
always delivers the credentials as an injected file (OpenBao init container on onprem, ESO
on aws and gcp), so the code change is the same shape for every pair. The default pair
aws → onprem keeps the original task fields byte for byte.
"""

import re
from dataclasses import dataclass
from typing import Literal

from .neutral.models import KINDS

# Store the app reads directly on each source kind, as named to the model.
SOURCE_SECRET_STORES = {"aws": "Secrets Manager", "gcp": "GCP Secret Manager", "onprem": "OpenBao"}


@dataclass(frozen=True)
class CredentialMigrationTask:
    run_id: str
    source_sha: str
    source_kind: Literal["aws", "gcp", "onprem"] = "aws"
    target_kind: Literal["aws", "gcp", "onprem"] = "onprem"

    def as_model_input(self) -> dict:
        """Return public task instructions, never a secret or runtime credential."""
        kinds = {} if (self.source_kind, self.target_kind) == ("aws", "onprem") else {
            "source_kind": self.source_kind, "target_kind": self.target_kind}
        return kinds | {
            "run_id": self.run_id,
            "source_sha": self.source_sha,
            "source_paths": ["app/db_secret.py", "app/config.py", "app/main.py"],
            "target_credential_file": "/run/bravo/database/credentials.json",
            "required_keys": ["username", "password"],
            "objective": f"Replace direct {SOURCE_SECRET_STORES[self.source_kind]} reads with injected credential file reads.",
            "requirements": [
                "Fail closed if the injected file is missing or invalid.",
                "Preserve the existing 300-second cache and refresh on authentication failure.",
                "Do not emit credential values in logs or proposal metadata.",
            ],
        }


def prepare_credential_migration(run_id: str, source_sha: str, *, source_kind: str = "aws",
                                 target_kind: str = "onprem") -> CredentialMigrationTask:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("invalid source revision")
    if source_kind not in KINDS or target_kind not in KINDS:
        raise ValueError("invalid environment kinds")
    return CredentialMigrationTask(run_id=run_id, source_sha=source_sha, source_kind=source_kind,
                                   target_kind=target_kind)


def same_task(task: CredentialMigrationTask) -> CredentialMigrationTask:
    """The task rebuilt from its own fields; callers compare it to reject a hand-made task."""
    return prepare_credential_migration(task.run_id, task.source_sha, source_kind=task.source_kind,
                                        target_kind=task.target_kind)
