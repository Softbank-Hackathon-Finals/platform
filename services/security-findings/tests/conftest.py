import hashlib

import pytest

# Synthetic per-test service credentials. Never valid outside the local TestClient.
READER_TOKEN = "test-reader-" + "r" * 40
WRITER_TOKEN = "test-writer-" + "w" * 40
READ_AUTH = {"Authorization": "Bearer " + READER_TOKEN}
WRITE_AUTH = {"Authorization": "Bearer " + WRITER_TOKEN}


def credential(token, now, *, environments=None, permissions=None, sources=None, subject="tester"):
    return {
        "credential_id": "credential-" + hashlib.sha256(token.encode()).hexdigest()[:12],
        "subject": subject,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "environments": environments or ["env-onprem"],
        "permissions": permissions or ["read"],
        "sources": sources or [],
        "not_before": now - 1,
        "expires_at": now + 3_600_000,
    }


@pytest.fixture
def service_credentials():
    def make(now):
        return [
            credential(READER_TOKEN, now),
            credential(
                WRITER_TOKEN,
                now,
                permissions=["ingest"],
                sources=["falco", "kyverno", "bravo_controls"],
            ),
        ]

    return make
