from dataclasses import replace
from types import SimpleNamespace

import pytest

from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.retry import AttemptPorts, RetryDecision, run_retries
from rehearsal_runner.runner import Namespace, ProposalRef


class Ports:
    def __init__(self, *, cleanup=True, wrong_uid=False):
        self.cleanup_result = cleanup
        self.wrong_uid = wrong_uid
        self.attempts = []
        self.events = []
        self.outcomes = []
        self.change = None

    def prepare(self, attempt, feedback_ref):
        self.attempts.append((attempt, feedback_ref))
        self.attempt = attempt
        return AttemptPorts(self, self, self, self)

    def propose(self, task):
        self.events.append("propose")
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)

    def create(self, run_id):
        self.events.append("create")
        return Namespace("rehearsal-test", "uid-1")

    def check(self, namespace):
        self.events.append("isolation")
        return IsolationPass(namespace.name, "wrong" if self.wrong_uid else namespace.uid, "probe-1")

    def verify_isolated(self, proposal, namespace, isolation):
        self.events.append("verify")
        return {"opaque": True}

    def cleanup(self, namespace):
        self.events.append("cleanup")
        return self.cleanup_result

    def assess(self, receipt, proposal, attempt):
        self.events.append("assess")
        decision = RetryDecision(proposal.run_id, proposal.source_sha, proposal.proposal_digest,
                                 attempt, self.outcomes.pop(0), "evidence-ref", f"verification-{attempt}")
        return replace(decision, **self.change) if self.change else decision


    def confirm(self, run_id, verification_id):
        self.events.append(("hold", run_id, verification_id))
        return True


def execute(ports):
    return run_retries(SimpleNamespace(run_id="run-1", source_sha="a" * 40), ports, ports, hold=ports)


def test_verified_retry_exhaustion_is_bounded_to_three_attempts():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED"] * 3
    result = execute(ports)
    assert (result.state, result.reason, result.attempts) == ("NEEDS_HUMAN", "RETRY_LIMIT_REACHED", 3)
    assert ports.attempts == [(1, None), (2, "evidence-ref"), (3, "evidence-ref")]
    assert ports.events.count("cleanup") == 3


def test_pass_after_retry_does_not_grant_approval():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED", "PASS"]
    result = execute(ports)
    assert (result.state, result.reason, result.attempts) == (
        "RECEIPT_UNVERIFIED", "APPROVAL_AUTHORITY_REQUIRED", 2)
    assert result.final_attempt.proposal.run_id == "run-1"


@pytest.mark.parametrize("change", [
    {"run_id": "other"}, {"source_sha": "c" * 40}, {"proposal_digest": "d" * 64},
    {"attempt": 2}, {"attempt": True},
])
def test_wrong_assessment_identity_never_retries(change):
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED"]
    ports.change = change
    result = execute(ports)
    assert result.reason == "RECEIPT_IDENTITY_MISMATCH"
    assert len(ports.attempts) == 1


def test_uncertain_cleanup_neither_assesses_nor_retries():
    ports = Ports(cleanup=False)
    result = execute(ports)
    assert result.reason == "CLEANUP_UNCONFIRMED"
    assert "assess" not in ports.events
    assert len(ports.attempts) == 1


def test_unknown_decision_is_human_handoff():
    ports = Ports()
    ports.outcomes = ["UNKNOWN"]
    result = execute(ports)
    assert result.reason == "VERIFICATION_REQUIRES_HUMAN"
    assert len(ports.attempts) == 1


def test_retry_requires_feedback_reference():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED"]
    ports.change = {"feedback_ref": None}
    assert execute(ports).reason == "RETRY_FEEDBACK_UNAVAILABLE"
    assert len(ports.attempts) == 1


def test_retry_does_not_start_until_previous_hold_is_confirmed():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED"]
    ports.confirm = lambda *_: False
    result = execute(ports)
    assert result.reason == "PREVIOUS_HOLD_UNCONFIRMED"
    assert ports.attempts == [(1, None)]
    assert ports.events == ["propose", "create", "isolation", "verify", "cleanup", "assess"]


def test_retry_confirms_each_previous_verification_before_next_attempt():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED", "RETRY_ALLOWED", "PASS"]
    result = execute(ports)
    assert result.attempts == 3
    holds = [event for event in ports.events if isinstance(event, tuple)]
    assert holds == [("hold", "run-1", "verification-1"), ("hold", "run-1", "verification-2")]
    assert ports.events.index(holds[0]) < ports.events.index(holds[1])


def test_retry_rejects_missing_verification_id():
    ports = Ports()
    ports.outcomes = ["RETRY_ALLOWED"]
    ports.change = {"verification_id": None}
    assert execute(ports).reason == "PREVIOUS_HOLD_UNCONFIRMED"
    assert len(ports.attempts) == 1


def test_stale_verifier_attempt_never_starts_namespace_or_job():
    ports = Ports()
    def stale(attempt, feedback_ref):
        ports.attempts.append((attempt, feedback_ref))
        ports.attempt = 2
        return AttemptPorts(ports, ports, ports, ports)
    ports.prepare = stale
    result = execute(ports)
    assert result.reason == "ATTEMPT_PREPARATION_UNAVAILABLE"
    assert result.attempts == 0
    assert ports.events == []


def test_stale_lifecycle_attempt_never_starts_namespace_or_job():
    ports = Ports()
    class StaleLifecycle:
        attempt = 2
    def stale(attempt, feedback_ref):
        ports.attempts.append((attempt, feedback_ref))
        ports.attempt = attempt
        return AttemptPorts(ports, StaleLifecycle(), ports, ports)
    ports.prepare = stale
    result = execute(ports)
    assert result.reason == "ATTEMPT_PREPARATION_UNAVAILABLE"
    assert result.attempts == 0 and ports.events == []
