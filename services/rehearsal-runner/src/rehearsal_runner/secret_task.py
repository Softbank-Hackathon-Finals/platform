"""Prepare metadata for a demo-chat credentials migration proposal."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialMigrationTask:
    run_id: str
    source_sha: str

    def as_model_input(self) -> dict:
        """Return public task instructions, never a secret or runtime credential."""
        return {
            "run_id": self.run_id,
            "source_sha": self.source_sha,
            "source_paths": ["app/db_secret.py", "app/config.py", "app/main.py"],
            "target_credential_file": "/run/bravo/database/credentials.json",
            "required_keys": ["username", "password"],
            "objective": "Replace direct Secrets Manager reads with injected credential file reads.",
            "requirements": [
                "Fail closed if the injected file is missing or invalid.",
                "Preserve the existing 300-second cache and refresh on authentication failure.",
                "Do not emit credential values in logs or proposal metadata.",
            ],
        }


def prepare_credential_migration(run_id: str, source_sha: str) -> CredentialMigrationTask:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("invalid source revision")
    return CredentialMigrationTask(run_id=run_id, source_sha=source_sha)
