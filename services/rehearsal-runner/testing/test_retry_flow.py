"""Local T15 simulation using T11 Coordinator and fabricated verifier receipts.

This does not authenticate evidence, persist attempts, or call the T38 verifier.
"""

import asyncio
import hashlib

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest, VerificationReceipt
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot


def coordinator_for(tmp_path):
    (tmp_path / "src").mkdir()
    original = 'DB_HOST = "localhost"\n'
    (tmp_path / "src/config.py").write_text(original)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    bundle = FailureBundle(run_id="run-1", source_sha="1" * 40,
                           failure_code="TEMPLATE_TEST_FAILED", summary="Rehearsal verification failed",
                           evidence_digest="a" * 64)
    manifest = SnapshotManifest(source_sha=bundle.source_sha, files={"src/config.py": original_digest},
                                editable_paths=["src/config.py"])
    proposal = {"run_id": bundle.run_id, "source_sha": bundle.source_sha,
                "rationale": "Use the configured host", "risk": "low",
                "edits": [{"path": "src/config.py", "before_sha256": original_digest,
                           "replacement": 'import os\nDB_HOST = os.environ["DB_HOST"]\n'}]}
    return Coordinator(bundle, Snapshot(tmp_path, manifest)), proposal


class FakeProvider:
    def __init__(self, proposal):
        self.proposal = proposal
        self.summaries = []

    async def propose(self, bundle, tools):
        self.summaries.append(bundle.summary)
        tools.dispatch("mcp__bravo__plan_propose", self.proposal)


class FakeVerifier:
    def __init__(self, verdict, **overrides):
        self.verdict = verdict
        self.overrides = overrides
        self.requests = []

    def submit(self, coordinator):
        result = coordinator.result()
        self.requests.append((result["run_id"], result["proposal_digest"], result["attempt"]))
        evidence = "e" * 64
        fields = {"run_id": result["run_id"], "source_sha": result["source_sha"],
                  "proposal_digest": result["proposal_digest"], "attempt": result["attempt"],
                  "verdict": self.verdict, "effects_known": True, "evidence_digest": evidence}
        if self.verdict == "FAIL":
            fields["failure"] = coordinator.bundle.model_copy(update={
                "summary": "Independent verifier reports a known failure", "evidence_digest": evidence})
        fields.update(self.overrides)
        return VerificationReceipt(**fields)


def run_attempt(coordinator, provider, verifier):
    result = asyncio.run(coordinator.propose(provider))
    assert result["state"] == "PROPOSAL_READY"
    return coordinator.verify(verifier.submit(coordinator))


def test_known_failures_retry_with_feedback_and_stop_at_three(tmp_path):
    coordinator, proposal = coordinator_for(tmp_path)
    provider, verifier = FakeProvider(proposal), FakeVerifier("FAIL")
    assert [run_attempt(coordinator, provider, verifier) for _ in range(3)] == [
        "RETRY_ALLOWED", "RETRY_ALLOWED", "NEEDS_HUMAN"]
    assert [request[2] for request in verifier.requests] == [1, 2, 3]
    assert provider.summaries[1:] == ["Independent verifier reports a known failure"] * 2
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(provider))
    assert len(verifier.requests) == 3


@pytest.mark.parametrize("verdict,overrides", [
    ("UNKNOWN", {}), ("PASS", {"effects_known": False}),
    ("PASS", {"run_id": "other-run"}), ("PASS", {"source_sha": "2" * 40}),
    ("PASS", {"proposal_digest": "b" * 64}), ("PASS", {"attempt": 2}),
])
def test_unknown_or_unbound_receipt_stops_without_retry(tmp_path, verdict, overrides):
    coordinator, proposal = coordinator_for(tmp_path)
    provider, verifier = FakeProvider(proposal), FakeVerifier(verdict, **overrides)
    assert run_attempt(coordinator, provider, verifier) == "NEEDS_HUMAN"
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(provider))
    assert len(verifier.requests) == 1


def test_known_failure_followed_by_pass_only_awaits_approval(tmp_path):
    coordinator, proposal = coordinator_for(tmp_path)
    provider = FakeProvider(proposal)
    assert run_attempt(coordinator, provider, FakeVerifier("FAIL")) == "RETRY_ALLOWED"
    assert run_attempt(coordinator, provider, FakeVerifier("PASS")) == "AWAITING_APPROVAL"
    assert coordinator.result()["attempt"] == 2


def test_failed_receipt_with_different_evidence_does_not_retry(tmp_path):
    coordinator, proposal = coordinator_for(tmp_path)
    provider = FakeProvider(proposal)
    asyncio.run(coordinator.propose(provider))
    verifier = FakeVerifier("FAIL")
    receipt = verifier.submit(coordinator)
    receipt = receipt.model_copy(update={"evidence_digest": "f" * 64})
    assert coordinator.verify(receipt) == "NEEDS_HUMAN"
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(provider))


def test_same_receipt_cannot_be_consumed_twice(tmp_path):
    coordinator, proposal = coordinator_for(tmp_path)
    provider, verifier = FakeProvider(proposal), FakeVerifier("FAIL")
    asyncio.run(coordinator.propose(provider))
    receipt = verifier.submit(coordinator)
    assert coordinator.verify(receipt) == "RETRY_ALLOWED"
    with pytest.raises(ValueError):
        coordinator.verify(receipt)
    assert coordinator.result()["attempt"] == 1
