import asyncio

import pytest

from bravo_agent.contracts import VerificationReceipt
from bravo_agent.coordinator import Coordinator


class TestProvider:
    __test__ = False

    def __init__(self, proposal):
        self.proposal = proposal
        self.seen_inputs = []

    async def propose(self, bundle, tools):
        self.seen_inputs.append(tools.model_input())
        tools.dispatch("mcp__bravo__repo_read", {"path": self.proposal["edits"][0]["path"]})
        tools.dispatch("mcp__bravo__plan_propose", self.proposal)


def receipt(coordinator, **changes):
    values = {"run_id": coordinator.bundle.run_id, "source_sha": coordinator.bundle.source_sha,
              "proposal_digest": coordinator.result()["proposal_digest"],
              "attempt": coordinator.attempt, "verdict": "PASS", "effects_known": True,
              "evidence_digest": "e" * 64}
    values.update(changes)
    if values["verdict"] == "FAIL":
        values["failure"] = coordinator.bundle.model_copy(update={
            "summary": "Independent validator reports a remaining destination failure",
            "evidence_digest": values["evidence_digest"],
        })
    return VerificationReceipt(**values)


def test_proposal_is_not_verification_or_approval(context):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    result = asyncio.run(coordinator.propose(TestProvider(proposal)))
    assert result["state"] == "PROPOSAL_READY"
    assert "verdict" not in result["proposal"]
    assert coordinator.verify(receipt(coordinator)) == "AWAITING_APPROVAL"
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(TestProvider(proposal)))


def test_known_failed_verification_stops_after_three_attempts(context):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    for attempt in range(1, 4):
        result = asyncio.run(coordinator.propose(TestProvider(proposal)))
        assert result["attempt"] == attempt
        if attempt > 1:
            assert result["input"]["summary"].startswith("Independent validator")
        state = coordinator.verify(receipt(coordinator, verdict="FAIL"))
        assert state == ("RETRY_ALLOWED" if attempt < 3 else "NEEDS_HUMAN")
    with pytest.raises(ValueError):
        asyncio.run(coordinator.propose(TestProvider(proposal)))
    assert len(coordinator.result()["attempts"]) == 3


@pytest.mark.parametrize("changes", [{"verdict": "UNKNOWN"}, {"effects_known": False},
                                     {"run_id": "other"}, {"source_sha": "2" * 40},
                                     {"proposal_digest": "b" * 64}, {"attempt": 2}])
def test_unknown_or_unbound_verification_stops_without_retry(context, changes):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    assert coordinator.verify(receipt(coordinator, **changes)) == "NEEDS_HUMAN"
    with pytest.raises(ValueError):
        coordinator.verify(receipt(coordinator))


def test_model_failure_is_not_automatically_retried_or_logged_raw(context):
    class BrokenProvider:
        calls = 0

        async def propose(self, bundle, tools):
            self.calls += 1
            raise RuntimeError("do-not-log-provider-secret")

    bundle, snapshot, _, _ = context
    provider = BrokenProvider()
    result = asyncio.run(Coordinator(bundle, snapshot).propose(provider))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None
    assert provider.calls == 1
    assert "do-not-log-provider-secret" not in str(result)


def test_free_text_success_without_proposal_is_rejected(context):
    class EmptyProvider:
        async def propose(self, bundle, tools):
            return "All tests passed!"

    bundle, snapshot, _, _ = context
    result = asyncio.run(Coordinator(bundle, snapshot).propose(EmptyProvider()))
    assert result["state"] == "NEEDS_HUMAN"


def test_audit_overflow_does_not_produce_an_approvable_proposal(context):
    bundle, snapshot, proposal, _ = context

    class NoisyProvider:
        async def propose(self, bundle, tools):
            for _ in range(130):
                tools.record("Bash", "DENIED", "TOOL_NOT_ALLOWED")
            tools.dispatch("mcp__bravo__plan_propose", proposal)

    result = asyncio.run(Coordinator(bundle, snapshot).propose(NoisyProvider()))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None
    assert result["attempts"][0]["audit_truncated"] is True


def test_retries_keep_the_original_task_next_to_the_failure_feedback(context):
    # 류지환 (11/5 12:38): on a retry the bundle becomes the verifier's feedback; the model must
    # still receive the original objective and allowed files of the run.
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    provider = TestProvider(proposal)
    asyncio.run(coordinator.propose(provider))
    coordinator.verify(receipt(coordinator, verdict="FAIL"))
    asyncio.run(coordinator.propose(provider))
    first, second = provider.seen_inputs
    assert first["task"] == second["task"] == bundle.model_dump()
    assert second["failure"]["summary"].startswith("Independent validator") and second["attempt"] == 2
    result = coordinator.result()
    assert result["task_digest"] == result["attempts"][0]["task_digest"] == result["attempts"][1]["task_digest"]
    assert result["input"]["summary"].startswith("Independent validator") and result["task"] == bundle.model_dump()
