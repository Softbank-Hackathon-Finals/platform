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


def test_t11_proposal_reaches_bucket_observation_with_same_identity(tmp_path):
    import io

    from rehearsal_runner.bucket_probe import ReadResult, observe_bucket_reads
    from rehearsal_runner.bucket_verifier import BucketVerifier

    content = b"known fixture"

    class Reader:
        def __init__(self, status):
            self.status = status

        def get(self, bucket, key):
            assert (bucket, key) == ("rehearsal-test", "canary")
            return ReadResult(self.status, io.BytesIO(content) if self.status == 200 else None)

    class Job:
        def __init__(self):
            self.request = None

        def verify(self, request):
            self.request = request
            return observe_bucket_reads(
                Reader(200), Reader(403), bucket=request.target_bucket, key="canary",
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )

    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    job, lifecycle = Job(), MockLifecycle()
    result = run_once(task, T11BucketProposer(coordinator, FakeProvider(), SOURCE_PATH, TARGET_PATH),
                      lifecycle, MockIsolation(), BucketVerifier(task, job))
    assert result.state == "RECEIPT_UNVERIFIED"
    assert result.untrusted_receipt.anonymous == "denied"
    assert job.request.proposal_digest == coordinator.result()["proposal_digest"]
    assert job.request.source_policy_sha256 == task.source_sha256
    assert job.request.namespace == Namespace("rehearsal-test", "uid-1")
    assert lifecycle.calls == [("create", "run-1"), ("cleanup", "uid-1")]


def test_actual_t11_proposal_can_be_resolved_by_its_digest(tmp_path):
    from rehearsal_runner.bucket_proposal_source import BoundBucketProposalSource
    from rehearsal_runner.bucket_verifier import BucketVerifier

    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)

    class Job:
        def verify(self, request):
            return BoundBucketProposalSource(coordinator, TARGET_PATH).resolve(request)

    outcome = run_once(task, T11BucketProposer(coordinator, FakeProvider(), SOURCE_PATH, TARGET_PATH),
                       MockLifecycle(), MockIsolation(), BucketVerifier(task, Job()))
    assert outcome.state == "RECEIPT_UNVERIFIED"
    assert outcome.untrusted_receipt.replacement == TARGET
    assert outcome.untrusted_receipt.proposal_digest == coordinator.result()["proposal_digest"]


def test_t11_bucket_adapter_resumes_second_and_third_attempt_with_verified_feedback(tmp_path):
    import asyncio

    from bravo_agent.contracts import VerificationReceipt

    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    provider = FakeProvider()
    first = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH).propose(task)
    for attempt in (1, 2):
        failure = coordinator.bundle.model_copy(update={
            "summary": "Independent verifier reports a known failure", "evidence_digest": "e" * 64,
        })
        receipt = VerificationReceipt(run_id=task.run_id, source_sha=task.source_sha,
                                      proposal_digest=first.proposal_digest, attempt=attempt,
                                      verdict="FAIL", effects_known=True, evidence_digest="e" * 64,
                                      failure=failure)
        assert coordinator.verify(receipt) == "RETRY_ALLOWED"
        proposer = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH,
                                     attempt=attempt + 1, feedback_digest="e" * 64)
        first = proposer.propose(task)
        assert coordinator.attempt == attempt + 1
    assert provider.calls == 3
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(provider))


def test_t11_bucket_retry_rejects_mismatched_feedback_before_model_call(tmp_path):
    from bravo_agent.contracts import VerificationReceipt

    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    provider = FakeProvider()
    first = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH).propose(task)
    failure = coordinator.bundle.model_copy(update={"evidence_digest": "e" * 64})
    receipt = VerificationReceipt(run_id=task.run_id, source_sha=task.source_sha,
                                  proposal_digest=first.proposal_digest, attempt=1,
                                  verdict="FAIL", effects_known=True, evidence_digest="e" * 64, failure=failure)
    assert coordinator.verify(receipt) == "RETRY_ALLOWED"
    with pytest.raises(ValueError, match="feedback mismatch"):
        T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH,
                          attempt=2, feedback_digest="f" * 64).propose(task)
    assert provider.calls == 1


def test_t15_runner_resumes_real_t11_after_known_failure_with_same_attempt_binding(tmp_path):
    """T11 coordinator is real; verification receipts are fixtures."""
    from bravo_agent.contracts import VerificationReceipt

    from rehearsal_runner.bucket_proposal_source import BoundBucketProposalSource
    from rehearsal_runner.bucket_verifier import BucketVerificationRequest
    from rehearsal_runner.integration_handoff import HandoffVerifier
    from rehearsal_runner.retry import AttemptPorts

    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="1" * 40)
    coordinator = make_coordinator(tmp_path, task)
    provider = FakeProvider()

    class Ports:
        def __init__(self):
            self.events = []
            self.index = 0

        def prepare(self, attempt, feedback_ref):
            self.events.append(("prepare", attempt, feedback_ref))
            proposer = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH,
                                         attempt=attempt, feedback_digest=feedback_ref)
            verifier = HandoffVerifier(self, attempt, b"fixture diff", "sha256:" + "c" * 64, "env-onprem")
            return AttemptPorts(proposer, self, self, verifier)

        def create(self, run_id):
            self.index += 1
            namespace = Namespace(f"rehearsal-{self.index}", f"uid-{self.index}")
            self.events.append(("create", namespace.uid))
            return namespace

        def check(self, namespace):
            return IsolationPass(namespace.name, namespace.uid, "probe-1")

        def verify(self, handoff):
            request = BucketVerificationRequest(task.run_id, task.source_sha, task.source_sha256,
                                                handoff.proposal.proposal_digest, task.target_bucket,
                                                handoff.namespace, handoff.attempt)
            proposal = BoundBucketProposalSource(coordinator, TARGET_PATH).resolve(request)
            assert proposal.proposal_digest == handoff.proposal.proposal_digest
            verdict = "FAIL" if handoff.attempt == 1 else "PASS"
            failure = (coordinator.bundle.model_copy(update={"evidence_digest": "e" * 64})
                       if verdict == "FAIL" else None)
            self.events.append(("job", handoff.attempt, handoff.namespace.uid))
            return VerificationReceipt(run_id=task.run_id, source_sha=task.source_sha,
                                       proposal_digest=handoff.proposal.proposal_digest, attempt=handoff.attempt,
                                       verdict=verdict, effects_known=True, evidence_digest="e" * 64,
                                       failure=failure)

        def cleanup(self, namespace):
            self.events.append(("cleanup", namespace.uid))
            return True

    ports = Ports()
    for attempt, feedback in [(1, None), (2, "e" * 64)]:
        prepared = ports.prepare(attempt, feedback)
        result = run_once(task, prepared.proposer, prepared.lifecycle, prepared.isolation, None,
                          isolated_verifier=prepared.verifier)
        assert result.state == "RECEIPT_UNVERIFIED"
        state = coordinator.verify(result.untrusted_receipt)
        assert state == ("RETRY_ALLOWED" if attempt == 1 else "AWAITING_APPROVAL")
    assert ports.events == [("prepare", 1, None), ("create", "uid-1"), ("job", 1, "uid-1"),
                            ("cleanup", "uid-1"), ("prepare", 2, "e" * 64),
                            ("create", "uid-2"), ("job", 2, "uid-2"), ("cleanup", "uid-2")]
    assert coordinator.attempt == 2 and provider.calls == 2


class CleanupAttemptProvider(FakeProvider):
    """Scripted agent that tries to delete the bucket before proposing."""

    async def propose(self, bundle, tools):
        from bravo_agent.tools import PolicyDenied

        with pytest.raises(PolicyDenied):
            tools.dispatch("rehearsal.exec", {"command": "aws s3 rb s3://fixture-bucket --force"})
        await super().propose(bundle, tools)


def test_attempt_tool_events_carry_the_blocked_delete_in_order_without_arguments(tmp_path):
    task = prepare_bucket_policy_task("run-1", SOURCE, "fixture-bucket", source_sha="a" * 40)
    coordinator = make_coordinator(tmp_path, task)
    proposer = T11BucketProposer(coordinator, CleanupAttemptProvider(), SOURCE_PATH, TARGET_PATH)
    proposer.propose(task)
    events = proposer.tool_events()
    assert [(e["tool"], e["reason"]) for e in events] == [
        ("rehearsal.exec", "DESTRUCTIVE_ATTEMPT_BLOCKED"), ("repo.read", "POLICY_ACCEPTED"),
        ("repo.read", "POLICY_ACCEPTED"), ("plan.propose", "POLICY_ACCEPTED")]
    assert events[0]["category"] == "bucket_delete"
    assert all(e["run_id"] == "run-1" and e["attempt"] == 1 for e in events)
    assert "fixture-bucket" not in repr(events)


def test_attempt_tool_events_exclude_other_attempts_and_verifier_records(tmp_path):
    from bravo_agent.contracts import VerificationReceipt

    task = prepare_bucket_policy_task("run-1", SOURCE, "fixture-bucket", source_sha="a" * 40)
    coordinator = make_coordinator(tmp_path, task)
    first = T11BucketProposer(coordinator, CleanupAttemptProvider(), SOURCE_PATH, TARGET_PATH)
    first.propose(task)
    failure = coordinator.bundle.model_copy(update={"evidence_digest": "e" * 64})
    coordinator.verify(VerificationReceipt(run_id=task.run_id, source_sha=task.source_sha,
                                           proposal_digest=coordinator.result()["proposal_digest"], attempt=1,
                                           verdict="FAIL", effects_known=True, evidence_digest="e" * 64,
                                           failure=failure))
    second = T11BucketProposer(coordinator, FakeProvider(), SOURCE_PATH, TARGET_PATH,
                               attempt=2, feedback_digest="e" * 64)
    second.propose(task)
    assert len(first.tool_events()) == 4
    assert [e["attempt"] for e in second.tool_events()] == [2, 2, 2]


def test_planner_resumes_t11_for_attempt_two_only_with_signed_failure(tmp_path):
    import base64
    from types import SimpleNamespace

    from rehearsal_runner.attempt_store import PlanBinding
    from rehearsal_runner.bucket_job import BucketFixture
    from rehearsal_runner.bucket_planner import BucketPlanner, BucketTarget

    task = prepare_bucket_policy_task("run-1", SOURCE, "fixture-bucket", source_sha="a" * 40)
    coordinator = make_coordinator(tmp_path, task)

    class Proposals:
        def open(self, admission, bound_task):
            return coordinator, FakeProvider()

    class Kube:
        def get(self, *args):
            return {"data": {"access": base64.b64encode(b"a").decode(), "secret": base64.b64encode(b"s").decode()}}

    profile = {f"{p}_{k}": v for p in ("SEAWEEDFS", "OPENBAO")
               for k, v in (("NAMESPACE", "ns"), ("LABEL_KEY", "app"), ("LABEL_VALUE", "x"), ("PORT", "1"))}
    planner = BucketPlanner(
        profile=profile, target=BucketTarget(
            BucketFixture("http://s:8333", "fixture-bucket", "canary", "a" * 64, "img@sha256:" + "b" * 64),
            "ns", "cred"),
        proposals=Proposals(), bindings=lambda _a: PlanBinding("c" * 64, "env"), kubectl=object(),
        kube=Kube(), isolation_image="busybox:1.36", source_policy_path=SOURCE_PATH,
        target_policy_path=TARGET_PATH)
    factory = planner.factory(SimpleNamespace(), task)
    factory.prepare(1, None).proposer.propose(task)
    with pytest.raises(ValueError, match="feedback"):
        factory.prepare(2, None)
    assert coordinator.state == "PROPOSAL_READY"
    second = factory.prepare(2, "e" * 64).proposer
    assert coordinator.state == "RETRY_ALLOWED" and coordinator.bundle.evidence_digest == "e" * 64
    second.propose(task)
    assert coordinator.attempt == 2 and coordinator.state == "PROPOSAL_READY"
