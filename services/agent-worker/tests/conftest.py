import hashlib

import pytest

from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.tools import Snapshot, Tools


@pytest.fixture
def context(tmp_path):
    (tmp_path / "src").mkdir()
    content = 'DB_HOST = "localhost"\n'
    (tmp_path / "src/config.py").write_text(content)
    sha = hashlib.sha256(content.encode()).hexdigest()
    manifest = SnapshotManifest(source_sha="1" * 40, files={"src/config.py": sha},
                                editable_paths=["src/config.py"])
    bundle = FailureBundle(run_id="run-1", source_sha="1" * 40,
                           failure_code="DB_CONNECTION_FAILED", summary="Destination DB unavailable",
                           evidence_digest="a" * 64)
    snapshot = Snapshot(tmp_path, manifest)
    proposal = {"run_id": bundle.run_id, "source_sha": bundle.source_sha,
                "rationale": "Read destination host from the injected environment", "risk": "medium",
                "edits": [{"path": "src/config.py", "before_sha256": sha,
                           "replacement": 'import os\nDB_HOST = os.environ["DB_HOST"]\n'}]}
    return bundle, snapshot, proposal, Tools(bundle, snapshot, 1)


# --- operator ledger with a synthetic carrier (the platform Job path is tested in platform-events) ---

def intent_for(body, mode="propose"):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from bravo_agent.product_execution_contracts import ExecutionIntent

    return ExecutionIntent(request_id="execution-1", input_ref=body["input_ref"], input_digest=body["input_digest"],
                           admission_digest="a" * 64, runtime_source_sha="c" * 40, mode=mode,
                           recipient_public_key=X25519PrivateKey.from_private_bytes(b"1" * 32)
                           .public_key().public_bytes_raw().hex())


@pytest.fixture
def dispatch(setup, tmp_path):
    """(carrier, carrier, intent, receipt): one recorded send per request, like the Job carrier."""
    import json as _json

    from bravo_agent.product_execution_contracts import ExecutionUnknown, receipt_binding
    from bravo_agent.product_ledger import OperatorLedger

    class Carrier(OperatorLedger):
        def __init__(self, root, receipt):
            super().__init__(root)
            self.receipt, self.posts = receipt, []

        def _bound(self, intent):
            intent = self._parse_intent(intent)
            if self._private_key(intent.request_id).public_key().public_bytes_raw().hex() != intent.recipient_public_key:
                raise ExecutionUnknown("EXECUTION_RECIPIENT_CONFLICT")
            return intent

        def submit(self, intent):
            intent = self._bound(intent)
            with self._db() as db:
                fresh = db.execute("INSERT OR IGNORE INTO executions VALUES (?,?,?,?)",
                                   (intent.request_id, _json.dumps(intent.model_dump()), "{}", "1234")).rowcount
            if fresh:
                self.posts.append(intent.request_id)
            return self.read(intent)

        def read(self, intent):
            intent = self._bound(intent)
            self._record(intent)
            return self.receipt

    service, _, body, _ = setup
    probe = Carrier(tmp_path / "state", None)
    intent = intent_for(body).model_copy(update={"recipient_public_key": probe.recipient("execution-1")})
    probe.receipt = receipt_binding(intent, 1234) | {"status": 200, "cleanup_confirmed": True,
                                                      "result": service.run(body)[1]}
    return probe, probe, intent, probe.receipt
