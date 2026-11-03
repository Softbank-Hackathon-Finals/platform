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
