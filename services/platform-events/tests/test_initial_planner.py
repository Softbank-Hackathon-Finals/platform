"""Actual Admission and existing T15 proposer/consumer; external effects are synthetic."""

import hashlib
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest
import test_initial_proposal as initial
from bravo_agent.contracts import SnapshotManifest
from bravo_agent.tools import Snapshot
from rehearsal_runner.credential_proposal_source import BoundCredentialProposalSource
from rehearsal_runner.credential_proposer import SOURCE_PATHS, T11CredentialProposer
from rehearsal_runner.credential_verifier import CredentialVerificationRequest
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.runner import Namespace
from rehearsal_runner.secret_task import prepare_credential_migration
from test_cutover import PRINCIPAL, SPEC
from test_workflow_recovery import ANALYSIS

from bravo_platform_events.initial_planner import StoredInitialCredentialSource
from bravo_platform_events.rehearsal import RehearsalService
from bravo_platform_events.store import EventError


@pytest.fixture
def flow(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(initial, "SOURCE_PATH", "app/db_secret.py")
    files = {path: "VALUE = 1\n" for path in SOURCE_PATHS}
    manifest = SnapshotManifest(source_sha=initial.SOURCE,
        files={path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()},
        editable_paths=list(SOURCE_PATHS))
    source = dict(schema_version=2, analysis=dict(inventory_digest=ANALYSIS.inventory_digest,
        item_id="item-001", grade="code_change", rationale="Synthetic credential migration analysis"),
        manifest=manifest.model_dump(mode="json"), files=files)
    f = initial.Fixture(backend, tmp_path, source_value=source)
    f.execute()
    f.publish()
    assert f.resume() == "REHEARSE"
    admissions = []
    host = T15RehearsalHost(f.catalog, SimpleNamespace(submit_once=lambda a, t: admissions.append(a)),
                           SimpleNamespace(read_authenticated=lambda _: None))
    service = RehearsalService(backend.reopen(), host=host, check_start=lambda *args: None,
                               first_run=f.execution().rehearsal_run_id)
    service.start(PRINCIPAL, SPEC.plan_id, task_id="credential", expected_version=4,
                  attempt=1, command_key="first")
    admission, = admissions
    root = tmp_path / "source"
    (root / "app").mkdir(parents=True)
    for path, text in files.items():
        (root / path).write_text(text)
    snapshot = Snapshot(root, manifest)
    for name in ("recipient", "submit", "read", "validate"):
        monkeypatch.setattr(f.client, name, lambda *args: pytest.fail("second producer call"))
    task = prepare_credential_migration(admission.rehearsal_id, admission.source_sha)
    return SimpleNamespace(f=f, admission=admission, task=task,
        snapshot=snapshot, source=StoredInitialCredentialSource(f.execution(), snapshot))


def propose(flow):
    coordinator, provider = flow.source.open(flow.admission, flow.task)
    proposer = T11CredentialProposer(coordinator, provider)
    reference = proposer.propose(flow.task)
    request = CredentialVerificationRequest(reference.run_id, reference.source_sha,
        reference.proposal_digest, Namespace("synthetic-rehearsal", "synthetic-uid"), 1)
    return coordinator, proposer, request


def boundary(flow, when):
    if when == "open":
        return partial(flow.source.open, flow.admission, flow.task)
    if when == "propose":
        coordinator, provider = flow.source.open(flow.admission, flow.task)
        return partial(T11CredentialProposer(coordinator, provider).propose, flow.task)
    coordinator, _, request = propose(flow)
    return partial(BoundCredentialProposalSource(coordinator).resolve, request)


def test_existing_t15_proposer_and_consumer_replay_same_proposal_after_restart(flow):
    for _ in range(2):
        # A new host adapter after restart reads the same stored result.
        flow.source = StoredInitialCredentialSource(flow.f.execution(), flow.snapshot)
        coordinator, proposer, request = propose(flow)
        stored = flow.f.execution().rehearsal_result(flow.admission)
        assert coordinator.result()["proposal"] == stored["proposal"]
        assert request.proposal_digest == stored["proposal_digest"]
        consumer = BoundCredentialProposalSource(coordinator)
        assert consumer.resolve(request)[0].path == "app/db_secret.py"
        assert consumer.resolve(request)[0].replacement == "VALUE = 2\n"
        with pytest.raises(ValueError, match="tool events"):
            proposer.tool_events()
    assert len(flow.f.client.submits) == 1 and flow.f.client.reads == []


@pytest.mark.parametrize("when", ["open", "propose", "consume"])
def test_revocation_is_checked_at_each_boundary(flow, when):
    operation = boundary(flow, when)
    flow.f.grant(False, 1)
    with pytest.raises(EventError):
        operation()


@pytest.mark.parametrize("when", ["open", "propose", "consume"])
def test_source_bytes_cannot_change_after_admission(flow, when):
    operation = boundary(flow, when)
    (flow.snapshot.root / "app/db_secret.py").write_text("VALUE = 9\n")
    with pytest.raises(ValueError):
        operation()


def test_same_sha_with_changed_manifest_is_not_the_admitted_snapshot(flow):
    manifest = flow.snapshot.manifest.model_copy(update={"editable_paths": ["app/db_secret.py"]})
    source = StoredInitialCredentialSource(flow.f.execution(), Snapshot(flow.snapshot.root, manifest))
    with pytest.raises(ValueError, match="input_changed"):
        source.open(flow.admission, flow.task)


@pytest.mark.parametrize("fault", ["task_run", "task_source", "uncommitted", "attempt"])
def test_wrong_task_or_admission_is_refused(flow, fault):
    admission, task = flow.admission, flow.task
    if fault == "task_run":
        task = replace(task, run_id="another-run")
    elif fault == "task_source":
        task = replace(task, source_sha="e" * 40)
    elif fault == "uncommitted":
        admission = replace(admission, task_id="uncommitted")
    else:
        admission = replace(admission, attempt=2)
    with pytest.raises((EventError, ValueError)):
        flow.source.open(admission, task)


def test_consumer_cannot_substitute_replayed_proposal(flow):
    coordinator, _, request = propose(flow)
    # Mutate the local coordinator only, not the authoritative stored result.
    proposal = coordinator._coordinator.proposal
    coordinator._coordinator.proposal = proposal.model_copy(update={"rationale": "changed"})
    with pytest.raises(ValueError, match="proposal_changed"):
        BoundCredentialProposalSource(coordinator).resolve(request)
