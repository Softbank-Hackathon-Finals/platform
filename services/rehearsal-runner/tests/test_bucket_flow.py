"""Local simulation only: no T11/T38 wire contract or production verifier."""

import hashlib
import io

import pytest

from rehearsal_runner.bucket_probe import ReadResult, observe_bucket_reads
from rehearsal_runner.bucket_task import prepare_bucket_policy_task

SOURCE = '{"Version":"2012-10-17","Statement":[]}'
CANARY = b"local test object"


class FakeReader:
    def __init__(self, status):
        self.status = status

    def get(self, bucket, key):
        assert (bucket, key) == ("rehearsal-test", "canary")
        return ReadResult(self.status, io.BytesIO(CANARY) if self.status == 200 else None)


class FakeAgent:
    def __init__(self, anonymous_status):
        self.anonymous_status = anonymous_status
        self.inputs = []

    def propose(self, task_input):
        self.inputs.append(task_input)
        return {"run_id": task_input["run_id"], "source_sha256": task_input["source_sha256"],
                "bucket": task_input["target_bucket"], "simulated_anonymous_status": self.anonymous_status}


class FakeVerifier:
    def __init__(self):
        self.requests = []

    def verify(self, task, proposal):
        if (proposal["run_id"] != task.run_id or proposal["source_sha256"] != task.source_sha256
                or proposal["bucket"] != task.target_bucket):
            raise ValueError("unbound test proposal")
        self.requests.append(proposal)
        return observe_bucket_reads(
            FakeReader(200), FakeReader(proposal["simulated_anonymous_status"]),
            bucket=task.target_bucket, key="canary", expected_sha256=hashlib.sha256(CANARY).hexdigest(),
        )


@pytest.mark.parametrize("status,anonymous", [(200, "allowed"), (403, "denied"), (500, "unknown")])
def test_task_proposal_and_independent_observation(status, anonymous):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="a" * 40)
    agent, verifier = FakeAgent(status), FakeVerifier()
    proposal = agent.propose(task.as_model_input())
    observation = verifier.verify(task, proposal)
    assert agent.inputs[0]["source_policy"] == SOURCE
    assert verifier.requests == [proposal]
    assert observation.authenticated_before == observation.authenticated_after == "matches"
    assert observation.anonymous == anonymous
    # Observations never claim a T38 receipt or grant approval.
    assert not hasattr(observation, "verdict")


@pytest.mark.parametrize("field", ["run_id", "source_sha256", "bucket"])
def test_fake_verifier_rejects_unbound_proposal(field):
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="a" * 40)
    proposal = FakeAgent(403).propose(task.as_model_input())
    proposal[field] = "different"
    verifier = FakeVerifier()
    with pytest.raises(ValueError, match="unbound"):
        verifier.verify(task, proposal)
    assert verifier.requests == []
