"""Credential source bytes and edit scope stay within the approved manifest."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bravo_agent.contracts import SnapshotManifest, digest
from bravo_agent.coordinator import Coordinator
from bravo_agent.product_contracts import ProductInput, validate_product_input
from bravo_agent.tools import Snapshot
from rehearsal_runner.bucket_proposer import validate_coordinator_attempt
from rehearsal_runner.credential_proposer import SOURCE_PATHS, credential_summary
from rehearsal_runner.secret_task import prepare_credential_migration

from bravo_platform_events.product_source import CredentialSnapshotSource


@pytest.fixture
def source(tmp_path):
    (tmp_path / "app").mkdir()
    files = {name: "import os\nvalue = os.environ.get('DATABASE_CREDENTIAL_FILE')\n" for name in SOURCE_PATHS}
    files["app/unused.py"] = "unused = True\n"
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    manifest = SnapshotManifest(source_sha="a" * 40,
        files={name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()},
        editable_paths=["app/db_secret.py", "app/unused.py"])
    return CredentialSnapshotSource(Snapshot(tmp_path, manifest))


def inputs():
    task = prepare_credential_migration("admitted-run", "a" * 40)
    return SimpleNamespace(rehearsal_id=task.run_id, source_sha=task.source_sha), task


def test_selected_bytes_pass_product_and_t15_initial_context_without_expanding_edits(source):
    admission, task = inputs()
    parts = source(None, None, admission, task)
    value = validate_product_input(ProductInput(admission_digest="b" * 64, attempt=1, **parts))
    assert set(value.files) == set(SOURCE_PATHS)
    assert value.manifest.editable_paths == ["app/db_secret.py"]
    assert value.bundle.evidence_digest == digest(value.manifest)
    coordinator = Coordinator(value.bundle, Snapshot(source.snapshot.root, value.manifest))
    validate_coordinator_attempt(coordinator, run_id=task.run_id, source_sha=task.source_sha,
        attempt=1, feedback_digest=None, initial_summary=credential_summary(task))
    assert coordinator.state == "READY" and coordinator.attempt == 0


@pytest.mark.parametrize("fault", ["revision", "run", "admission-source", "not-editable", "missing", "tampered", "credential"])
def test_invalid_source_or_authority_cannot_produce_input(source, fault):
    admission, task = inputs()
    path = source.snapshot.root / "app/db_secret.py"
    if fault == "revision":
        task = replace(task, source_sha="c" * 40)
    elif fault == "run":
        task = replace(task, run_id="other-run")
    elif fault == "admission-source":
        admission.source_sha = "c" * 40
    elif fault == "not-editable":
        manifest = source.snapshot.manifest.model_copy(update={"editable_paths": ["app/main.py"]})
        source = CredentialSnapshotSource(Snapshot(source.snapshot.root, manifest))
    elif fault == "missing":
        path.unlink()
    else:
        value = 'DB_PASSWORD = "synthetic-password-123456"\n' if fault == "credential" else "changed\n"
        path.write_text(value)
        if fault == "credential":
            # Even an approved matching hash does not permit credential literals.
            manifest = source.snapshot.manifest.model_copy(update={"files": source.snapshot.manifest.files |
                {"app/db_secret.py": hashlib.sha256(value.encode()).hexdigest()}})
            source = CredentialSnapshotSource(Snapshot(source.snapshot.root, manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        source(None, None, admission, task)
