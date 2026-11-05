"""Read real Git objects, synthetic authenticated GitHub responses, real journals."""

import hashlib
import io
import json
import subprocess
import zipfile
from copy import deepcopy
from dataclasses import replace

import pytest
from bravo_agent.contracts import Proposal, digest

from bravo_platform_events import candidate_publication as candidate
from bravo_platform_events.initial_proposal import InitialInput, InitialIntent, InitialResult


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Synthetic fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app").mkdir()
    (root / "app/config.py").write_text("VALUE = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "source")
    base = git(root, "rev-parse", "HEAD")
    (root / "app/config.py").write_text("VALUE = 2\n")
    git(root, "commit", "-qam", "candidate")
    return root, base, git(root, "rev-parse", "HEAD")


class Provider:
    def __init__(self, selected):
        self.selected, self.responses, self.archives, self.calls = selected, {}, {}, []
        for attempt in {selected.build_attempt, selected.ghcr_attempt, selected.ecr_attempt}:
            path = f"repos/{candidate.REPOSITORY}/actions/runs/{selected.run_id}/attempts/{attempt}"
            self.responses[path] = dict(id=selected.run_id, run_attempt=attempt,
                workflow_id=selected.workflow_id, repository=dict(full_name=candidate.REPOSITORY),
                head_repository=dict(full_name=candidate.REPOSITORY), head_sha=selected.workflow_sha,
                head_branch="main", path=candidate.WORKFLOW, event="workflow_dispatch",
                status="completed", conclusion="success")
            jobs = [dict(name=name, run_id=selected.run_id, run_attempt=attempt,
                head_sha=selected.workflow_sha, status="completed", conclusion="success")
                for name in ("build", "ghcr", "ecr")]
            self.responses[path + "/jobs?per_page=100"] = dict(total_count=3, jobs=jobs)

    def archive(self, identity, name, filename=None, value=None):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(filename or "fixture", json.dumps(value or {}))
        raw = data.getvalue()
        self.archives[identity] = raw
        self.responses[f"repos/{candidate.REPOSITORY}/actions/artifacts/{identity}"] = dict(
            id=identity, name=name, expired=False, size_in_bytes=len(raw),
            digest="sha256:" + hashlib.sha256(raw).hexdigest(), workflow_run=dict(
                id=self.selected.run_id, head_sha=self.selected.workflow_sha))

    def get(self, path):
        self.calls.append(("get", path))
        return deepcopy(self.responses[path])

    def download(self, identity):
        self.calls.append(("download", identity))
        return self.archives[identity]


class Bundle:
    def __init__(self, repository, *, retry=False):
        self.root, base, head = repository
        self.selected = candidate.PublicationSelection(workflow_sha="c" * 40, workflow_id=7,
            run_id=101, build_attempt=1, ghcr_attempt=1, ecr_attempt=2 if retry else 1,
            build_artifact_id=201, ghcr_artifact_id=202, ecr_artifact_id=203,
            candidate_sha=head, pull_request=9)
        content = "VALUE = 1\n"
        before = hashlib.sha256(content.encode()).hexdigest()
        self.value = InitialInput(schema_version=2,
            analysis=dict(inventory_digest="f" * 64, item_id="item-001", grade="code_change",
                          rationale="Synthetic credential migration analysis"),
            binding=dict(plan_id="plan-one", tenant_id="tenant-one",
            plan_digest="a" * 64, target_environment_id="target-one", plan_version=3,
            task_id="credential", task_digest="d" * 64, source_sha=base,
            collection_scope_digest="e" * 64, inventory_digest="f" * 64,
            comparison_digest="1" * 64, analysis_receipt_id="analysis-one"), run_id="run-one",
            manifest=dict(source_sha=base, files={"app/config.py": before},
                          editable_paths=["app/config.py"]), files={"app/config.py": content})
        self.intent = InitialIntent(request_id="t10p-" + "2" * 64, plan_id="plan-one", plan_version=3,
            input_ref="initial-one", input_digest=digest(self.value), binding_digest=digest(self.value.binding),
            runtime_source_sha="3" * 40, recipient_public_key="4" * 64)
        proposal = Proposal(run_id="run-one", source_sha=base, rationale="Synthetic replacement", risk="low",
            edits=[dict(path="app/config.py", before_sha256=before, replacement="VALUE = 2\n")])
        self.result = InitialResult(request_id=self.intent.request_id, input_digest=self.intent.input_digest,
            runtime_source_sha=self.intent.runtime_source_sha, execution_receipt_id="model-fixture",
            proposal=proposal, proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)
        self.provider = Provider(self.selected)
        self.receipt = dict(schema_version=1, repository=candidate.REPOSITORY, base_sha=base,
            source_sha=head, workflow_sha=self.selected.workflow_sha, pull_request=9,
            diff_sha256=candidate.CandidateGit(self.root).verify(self.value, self.result, self.selected),
            request_id=candidate.publication_request_id(self.intent), workflow_run_id="101",
            workflow_run_attempt="1", image=candidate.IMAGE, build_artifact_id="201",
            publisher_run_attempt="1", destination="ghcr-private", tag="candidate-101-1",
            scanned_before_publish=True, vulnerability_count=0, secret_match_count=0,
            runtime_packaging_probe_verified=False, runtime_probe_evidence="candidate_process_self_report",
            runtime_probe_self_report="pass", runtime_probe_in_build_job_only=True,
            candidate_executed_on_runner=False, all_blobs_identical=True, verified_blob_count=15,
            candidate_executed_in_publisher=False, rebuilt=False, t15_rehearsal_verified=False,
            t31_deployment_verified=False, digest="sha256:" + "5" * 64)
        self.reseal()

    def reseal(self, changes=None):
        self.provider.archive(201, "candidate-bundle-101-1")
        ghcr = self.receipt | (changes or {})
        ecr = ghcr | dict(destination="aws-ecr-private", publisher_run_attempt=str(self.selected.ecr_attempt))
        self.provider.archive(202, "candidate-ghcr-receipt-101-1", "ghcr-candidate-receipt.json", ghcr)
        self.provider.archive(203, f"candidate-ecr-receipt-101-{self.selected.ecr_attempt}",
                              "ecr-candidate-receipt.json", ecr)

    def prepare(self):
        return candidate.prepare_publication(value=self.value, intent=self.intent, result=self.result,
            selection=self.selected, repository=self.root, github=self.provider)


@pytest.fixture
def bundle(repository):
    return Bundle(repository)


def test_same_proposal_maps_to_authenticated_publication_without_calls_in_reader(bundle):
    b = bundle
    reader = b.prepare()
    before = list(b.provider.calls)
    record = reader(None, b.value, b.intent, b.result)
    assert record.request_id == b.intent.request_id
    assert len(candidate.publication_request_id(b.intent)) == 64
    assert record.source_sha == b.value.binding.source_sha
    assert record.image_digest == b.receipt["digest"]
    assert record.diff_hash == "sha256:" + b.receipt["diff_sha256"]
    assert reader(None, b.value, b.intent, b.result) == record
    assert b.provider.calls == before
    assert ("download", 201) not in before  # No image archive pulled by T10.


def test_successful_publisher_retry_keeps_original_build_identity(repository):
    b = Bundle(repository, retry=True)
    # Attempt 1 may fail in ECR; its successful build/GHCR remain the producers.
    b.provider.responses[f"repos/{candidate.REPOSITORY}/actions/runs/101/attempts/1"]["conclusion"] = "failure"
    assert b.prepare()(None, b.value, b.intent, b.result).image_digest == b.receipt["digest"]


@pytest.mark.parametrize("change", [
    {"request_id": "fixture-request"}, {"base_sha": "6" * 40}, {"source_sha": "7" * 40},
    {"diff_sha256": "8" * 64}, {"workflow_sha": "9" * 40}, {"workflow_run_attempt": "2"},
    {"build_artifact_id": "999"}, {"vulnerability_count": 1}, {"vulnerability_count": False},
    {"scanned_before_publish": 1}, {"all_blobs_identical": False}, {"rebuilt": True},
    {"candidate_executed_in_publisher": True}, {"runtime_packaging_probe_verified": True},
    {"t15_rehearsal_verified": True}, {"verified_blob_count": True}, {"digest": "invalid"},
])
def test_authenticated_but_inconsistent_receipt_is_rejected(bundle, change):
    bundle.reseal(change)
    with pytest.raises(ValueError):
        bundle.prepare()


@pytest.mark.parametrize("destination", ["ghcr", "ecr"])
@pytest.mark.parametrize("field,value", [
    ("digest", "sha256:" + "6" * 64), ("verified_blob_count", 16),
])
def test_one_publisher_receipt_cannot_disagree_with_the_other(bundle, destination, field, value):
    identity = 202 if destination == "ghcr" else 203
    with zipfile.ZipFile(io.BytesIO(bundle.provider.archives[identity])) as archive:
        receipt = json.loads(archive.read(f"{destination}-candidate-receipt.json"))
    receipt[field] = value
    # Re-authenticate just this archive so only the cross-publisher check rejects it.
    bundle.provider.archive(identity, f"candidate-{destination}-receipt-101-1",
                            f"{destination}-candidate-receipt.json", receipt)
    with pytest.raises(ValueError):
        bundle.prepare()


@pytest.mark.parametrize("field,value", [
    ("head_sha", "a" * 40), ("head_branch", "candidate"), ("event", "pull_request"),
    ("path", ".github/workflows/other.yml"), ("workflow_id", True),
    ("run_attempt", 2), ("repository", {"full_name": "other/demo-chat"}),
    ("status", "in_progress"), ("conclusion", "failure"),
])
def test_provider_run_identity_and_success_are_required(bundle, field, value):
    bundle.provider.responses[f"repos/{candidate.REPOSITORY}/actions/runs/101/attempts/1"][field] = value
    with pytest.raises(ValueError):
        bundle.prepare()


@pytest.mark.parametrize("field,value", [
    ("expired", True), ("digest", "sha256:" + "a" * 64), ("name", "unrelated"),
    ("workflow_run", {"id": 102, "head_sha": "c" * 40}), ("size_in_bytes", 2**30),
])
def test_receipt_archive_metadata_is_authenticated(bundle, field, value):
    bundle.provider.responses[f"repos/{candidate.REPOSITORY}/actions/artifacts/202"][field] = value
    with pytest.raises(ValueError):
        bundle.prepare()


def test_missing_or_truncated_jobs_never_prove_success(bundle):
    data = bundle.provider.responses[f"repos/{candidate.REPOSITORY}/actions/runs/101/attempts/1/jobs?per_page=100"]
    data["jobs"].pop()
    with pytest.raises(ValueError):
        bundle.prepare()


def test_extra_unseen_jobs_cannot_prove_unique_success(bundle):
    data = bundle.provider.responses[f"repos/{candidate.REPOSITORY}/actions/runs/101/attempts/1/jobs?per_page=100"]
    data["total_count"] = len(data["jobs"]) + 1
    with pytest.raises(ValueError):
        bundle.prepare()


def test_same_candidate_tree_without_base_ancestry_is_rejected(bundle):
    b = bundle
    tree = git(b.root, "rev-parse", b.selected.candidate_sha + "^{tree}")
    unrelated = git(b.root, "commit-tree", tree, "-m", "independent candidate root")
    b.selected = replace(b.selected, candidate_sha=unrelated)
    b.receipt["source_sha"] = unrelated
    b.reseal()
    with pytest.raises(ValueError):
        b.prepare()
    assert b.provider.calls == []


@pytest.mark.parametrize("mutation", ["extra_file", "different_replacement", "mode", "symlink"])
def test_actual_git_candidate_must_be_exact_saved_proposal(bundle, mutation):
    b = bundle
    if mutation == "extra_file":
        (b.root / "app/other.py").write_text("EXTRA = True\n")
    elif mutation == "different_replacement":
        (b.root / "app/config.py").write_text("VALUE = 3\n")
    elif mutation == "mode":
        (b.root / "app/config.py").chmod(0o755)
    else:
        (b.root / "app/config.py").unlink()
        (b.root / "app/config.py").symlink_to("other.py")
    git(b.root, "add", ".")
    git(b.root, "commit", "-qm", "unproposed mutation")
    b.selected = replace(b.selected, candidate_sha=git(b.root, "rev-parse", "HEAD"))
    with pytest.raises(ValueError):
        b.prepare()
    assert b.provider.calls == []


def test_prepared_reader_is_frozen_and_rejects_other_saved_input(bundle):
    b = bundle
    reader = b.prepare()
    b.receipt["digest"] = "sha256:" + "6" * 64
    b.reseal()
    assert reader(None, b.value, b.intent, b.result).image_digest == "sha256:" + "5" * 64
    changed = b.intent.model_copy(update={"input_ref": "initial-other"})
    with pytest.raises(ValueError):
        reader(None, b.value, changed, b.result)


def test_t10_request_and_input_digest_must_match_before_provider_read(bundle):
    bundle.intent = bundle.intent.model_copy(update={"binding_digest": "7" * 64})
    with pytest.raises(ValueError):
        bundle.prepare()
    assert bundle.provider.calls == []


@pytest.mark.parametrize("broken", ["duplicate_entry", "wrong_name", "symlink", "duplicate_key", "oversize"])
def test_authenticated_archive_is_bounded_and_unambiguous(bundle, broken):
    b = bundle
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        name = "ghcr-candidate-receipt.json"
        raw = json.dumps(b.receipt)
        if broken == "duplicate_key":
            raw = raw[:-1] + ', "digest": "duplicate"}'
        if broken == "oversize":
            raw = "x" * (candidate.MAX_RECEIPT + 1)
        info = zipfile.ZipInfo("../" + name if broken == "wrong_name" else name)
        if broken == "symlink":
            info.external_attr = 0o120777 << 16
        archive.writestr(info, raw)
        if broken == "duplicate_entry":
            archive.writestr("extra.json", "{}")
    raw = stream.getvalue()
    b.provider.archives[202] = raw
    metadata = b.provider.responses[f"repos/{candidate.REPOSITORY}/actions/artifacts/202"]
    metadata.update(size_in_bytes=len(raw), digest="sha256:" + hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        b.prepare()


@pytest.mark.parametrize("revoked", [False, True])
def test_real_t10_journal_rechecks_current_grant_after_publication_prepare(
        backend, repository, tmp_path, monkeypatch, revoked):
    import test_initial_proposal as original
    from test_cutover import PRINCIPAL, SPEC
    from test_workflow_recovery import service

    from bravo_platform_events.store import EventError
    from bravo_platform_events.workflow import WorkflowRecovery

    b = Bundle(repository)
    monkeypatch.setattr(original, "SOURCE", repository[1])
    f = original.Fixture(backend, tmp_path)
    f.execute()
    b.value, b.intent, b.result = f.port.publication_inputs(SPEC.plan_id, 3, **f.ref)
    b.receipt["request_id"] = candidate.publication_request_id(b.intent)
    b.reseal()
    publication = b.prepare()
    calls = list(b.provider.calls)
    workflow = WorkflowRecovery(service(backend.store, None),
        read_evidence=f.port.evidence_reader(publication=publication))
    if revoked:
        f.grant(False, 1)
        with pytest.raises(EventError):
            f.port.publication_inputs(SPEC.plan_id, 3, **f.ref)
        with pytest.raises(EventError):
            workflow.resume_once(PRINCIPAL, SPEC.plan_id)
        assert backend.store.snapshot(SPEC.plan_id)["state"] == "PROPOSE"
    else:
        assert workflow.resume_once(PRINCIPAL, SPEC.plan_id) == "REHEARSE"
        assert f.port.stored(SPEC.plan_id, 3, **f.ref)["proposal_digest"] == b.result.proposal_digest
        with pytest.raises(EventError):
            f.port.publication_inputs(SPEC.plan_id, 3, **f.ref)
    assert b.provider.calls == calls
    assert len(f.client.submits) == 1
