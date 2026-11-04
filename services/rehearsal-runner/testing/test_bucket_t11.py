"""T15 bucket task → actual T11 validation → mock isolated verification."""

import hashlib
from pathlib import Path

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot

from rehearsal_runner.bucket_proposer import T11BucketProposer, bucket_summary
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, run_once

SOURCE = '{"Statement":[{"Effect":"Deny","Action":"s3:GetObject","Principal":"*","Resource":"fixture"}]}'
TARGET = '{"Statement":[{"Effect":"Deny","Action":"s3:GetObject","Principal":"*","Resource":"fixture"}]}'
SOURCE_PATH = "src/source-policy.json"
TARGET_PATH = "src/target-policy.json"
OTHER_PATH = "src/other-policy.json"


class FakeProvider:
    def __init__(self, replacement=TARGET, target_path=TARGET_PATH):
        self.replacement = replacement
        self.target_path = target_path
        self.calls = 0

    async def propose(self, bundle, tools):
        self.calls += 1
        tools.dispatch("mcp__bravo__repo_read", {"path": SOURCE_PATH})
        target = tools.dispatch("mcp__bravo__repo_read", {"path": self.target_path})
        tools.dispatch("mcp__bravo__plan_propose", {
            "run_id": bundle.run_id, "source_sha": bundle.source_sha,
            "rationale": "Translate scoped policy", "risk": "low",
            "edits": [{"path": self.target_path, "before_sha256": target["sha256"],
                       "replacement": self.replacement}],
        })


def make_coordinator(tmp_path: Path, task, *, summary=None):
    (tmp_path / "src").mkdir()
    (tmp_path / SOURCE_PATH).write_text(SOURCE)
    (tmp_path / TARGET_PATH).write_text(TARGET)
    (tmp_path / OTHER_PATH).write_text(TARGET)
    files = {path: hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
             for path in (SOURCE_PATH, TARGET_PATH, OTHER_PATH)}
    manifest = SnapshotManifest(source_sha=task.source_sha, files=files, editable_paths=[TARGET_PATH, OTHER_PATH])
    bundle = FailureBundle(run_id=task.run_id, source_sha=task.source_sha,
                           failure_code="POLICY_TRANSLATION", evidence_digest="c" * 64,
                           summary=summary or bucket_summary(task, SOURCE_PATH, TARGET_PATH))
    return Coordinator(bundle, Snapshot(tmp_path, manifest))


class MockLifecycle:
    def __init__(self):
        self.calls = []

    def create(self, run_id):
        self.calls.append(("create", run_id))
        return Namespace("rehearsal-test", "uid-1")

    def cleanup(self, namespace):
        self.calls.append(("cleanup", namespace.uid))
        return True


class MockIsolation:
    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")


class MockVerifier:
    def __init__(self):
        self.calls = []

    def verify(self, proposal, namespace):
        self.calls.append((proposal, namespace))
        return {"test_only": "untrusted receipt"}


def test_task_uses_t11_validated_proposal_then_mock_verification(tmp_path):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    provider, lifecycle, verifier = FakeProvider(), MockLifecycle(), MockVerifier()
    result = run_once(task, T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH),
                      lifecycle, MockIsolation(), verifier)
    assert result.state == "RECEIPT_UNVERIFIED"
    assert provider.calls == 1 and coordinator.state == "PROPOSAL_READY"
    assert verifier.calls[0][0].proposal_digest == coordinator.result()["proposal_digest"]
    assert verifier.calls[0][1] == Namespace("rehearsal-test", "uid-1")
    assert lifecycle.calls == [("create", "run-1"), ("cleanup", "uid-1")]


@pytest.mark.parametrize("fault", ["wrong_source", "wrong_summary", "unsafe_proposal"])
def test_mismatch_or_t11_rejection_never_creates_namespace(tmp_path, fault):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task, summary="unexpected" if fault == "wrong_summary" else None)
    if fault == "wrong_source":
        (tmp_path / SOURCE_PATH).write_text("changed")
    provider = FakeProvider("delete_bucket()" if fault == "unsafe_proposal" else TARGET)
    lifecycle, verifier = MockLifecycle(), MockVerifier()
    result = run_once(task, T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH),
                      lifecycle, MockIsolation(), verifier)
    assert result.state == "NEEDS_HUMAN"
    assert lifecycle.calls == [] and verifier.calls == []


def test_t11_valid_but_other_editable_file_never_reaches_job(tmp_path):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    provider = FakeProvider(target_path=OTHER_PATH)
    lifecycle, verifier = MockLifecycle(), MockVerifier()
    result = run_once(task, T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH),
                      lifecycle, MockIsolation(), verifier)
    assert coordinator.state == "PROPOSAL_READY"
    assert result.state == "NEEDS_HUMAN"
    assert lifecycle.calls == [] and verifier.calls == []


def test_same_source_and_target_path_is_rejected_before_provider(tmp_path):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    coordinator.bundle = coordinator.bundle.model_copy(update={
        "summary": bucket_summary(task, SOURCE_PATH, SOURCE_PATH),
    })
    provider, lifecycle, verifier = FakeProvider(), MockLifecycle(), MockVerifier()
    result = run_once(task, T11BucketProposer(coordinator, provider, SOURCE_PATH, SOURCE_PATH),
                      lifecycle, MockIsolation(), verifier)
    assert result.state == "NEEDS_HUMAN"
    assert provider.calls == 0 and lifecycle.calls == [] and verifier.calls == []
