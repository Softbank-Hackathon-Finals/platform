import hashlib
from datetime import UTC, datetime

import pytest

from bravo_apply.binding import diff_hash, render_diff

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
IMAGE = "sha256:" + "a" * 64
REPO = "ghcr.io/example/demo-chat"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_now(monkeypatch):
    """CLI는 실제 현재 시각으로 승인 만료를 본다. 고정된 시험 승인(만료 10:00Z)이 날짜에 따라 깨지지 않게 NOW로 맞춘다."""
    from bravo_apply import cli, path, policy

    for module in (cli, path, policy):
        monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    return NOW


BEFORE = "import boto3\n\ndef creds():\n    return boto3.client('secretsmanager').get_secret_value(SecretId='demo-chat/db')\n"
AFTER = "import json\n\ndef creds():\n    with open('/run/bravo/database/credentials.json') as f:\n        return json.load(f)\n"


@pytest.fixture
def sources():
    return {"app/db_secret.py": BEFORE}


@pytest.fixture
def proposal():
    return {"schema_version": 1, "run_id": "run-demo-1", "source_sha": "1" * 40, "rationale": "read DB credentials from the injected file",
            "risk": "medium", "edits": [{"path": "app/db_secret.py", "before_sha256": hashlib.sha256(BEFORE.encode()).hexdigest(),
                                         "replacement": AFTER}]}


@pytest.fixture
def claim(proposal, sources):
    return {"application_id": "app-0001", "approval_id": "approval-0001", "verification_id": "verify-0001",
            "rehearsal_id": "run-demo-1", "claimed_by": "operator-1", "claimed_at": "2026-11-06T08:55:00Z", "context_revision": 3,
            "binding": {"plan_id": "plan-demo", "plan_digest": "c" * 64, "diff_hash": diff_hash(render_diff(sources, proposal["edits"])),
                        "image_digest": IMAGE, "target_environment_id": "onprem-dev", "expires_at": "2026-11-06T10:00:00Z"}}
