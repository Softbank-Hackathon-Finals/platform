from dataclasses import replace

import pytest

from rehearsal_runner.approval_handoff import prepare_candidate
from rehearsal_runner.integration_handoff import prepare_handoff
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef


def handoff():
    return prepare_handoff(ProposalRef("run-1", "a" * 40, "b" * 64), Namespace("rehearsal-test", "uid-1"),
                           IsolationPass("rehearsal-test", "uid-1", "probe-1"), attempt=1, diff=b"fixture diff",
                           image_digest="sha256:" + "c" * 64, target_environment_id="env-onprem")


def test_candidate_binds_expected_identities_without_granting_approval():
    candidate = prepare_candidate(handoff(), plan_id="plan-1", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=True)
    assert (candidate.run_id, candidate.attempt, candidate.verification_id) == ("run-1", 1, "verification-1")
    assert candidate.namespace_uid == candidate.cleanup_uid == "uid-1"
    assert not hasattr(candidate, "binding")


@pytest.mark.parametrize("values", [
    {"cleanup_uid": "uid-other"}, {"cleanup_confirmed": False}, {"cleanup_confirmed": 1},
    {"evidence_ref": ""}, {"evidence_ref": "https://example.test/?token=secret"},
    {"verification_id": ""}, {"plan_id": "../plan"},
])
def test_mismatches_and_missing_references_fail_closed(values):
    args = dict(plan_id="plan-1", verification_id="verification-1", evidence_ref="evidence-1",
                cleanup_uid="uid-1", cleanup_confirmed=True)
    args.update(values)
    with pytest.raises(ValueError):
        prepare_candidate(handoff(), **args)


def test_candidate_changes_if_verified_artifact_changes():
    original = handoff()
    args = dict(plan_id="plan-1", verification_id="verification-1", evidence_ref="evidence-1",
                cleanup_uid="uid-1", cleanup_confirmed=True)
    first = prepare_candidate(original, **args)
    second = prepare_candidate(replace(original, image_digest="sha256:" + "d" * 64), **args)
    assert first != second


def test_t10_fields_omit_local_cleanup_assertions():
    candidate = prepare_candidate(handoff(), plan_id="plan-1", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=True)
    fields = candidate.t10_fields()
    assert set(fields) == {"plan_id", "run_id", "attempt", "verification_id", "evidence_ref",
                           "target_environment_id", "namespace", "proposal_digest", "diff_hash", "image_digest"}
    assert "namespace_uid" not in fields and "cleanup_uid" not in fields


@pytest.mark.parametrize("value", ["plan.with.dots", "plan:colon"])
def test_identifiers_follow_t10_intake_rules(value):
    with pytest.raises(ValueError):
        prepare_candidate(handoff(), plan_id=value, verification_id="verification-1", evidence_ref="evidence-1",
                          cleanup_uid="uid-1", cleanup_confirmed=True)


def test_submit_invokes_only_configured_intake_and_returns_acknowledgement():
    from rehearsal_runner.approval_handoff import submit_candidate

    candidate = prepare_candidate(handoff(), plan_id="plan-1", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=True)
    class Candidate:
        def __init__(self, **fields):
            self.fields = fields
        def validate(self):
            assert "cleanup_uid" not in self.fields
    class Intake:
        def __init__(self):
            self.calls = 0
        def receive(self, token, value):
            self.calls += 1
            assert token == "fixture-token" and isinstance(value, Candidate)
            return {"verification_id": "verification-1", "attempt": 1, "revision": "a" * 64}
    intake = Intake()
    result = submit_candidate(candidate, candidate_type=Candidate, intake=intake,
                              token_supplier=lambda: "fixture-token")
    assert (result.state, result.revision, intake.calls) == ("ACKNOWLEDGED", "a" * 64, 1)
    assert "fixture-token" not in repr(result)


@pytest.mark.parametrize("receipt", [
    None, {}, {"verification_id": "other", "attempt": 1, "revision": "a" * 64},
    {"verification_id": "verification-1", "attempt": True, "revision": "a" * 64},
    {"verification_id": "verification-1", "attempt": 1, "revision": "bad"},
])
def test_unknown_or_unbound_acknowledgement_never_claims_success(receipt):
    from rehearsal_runner.approval_handoff import submit_candidate

    candidate = prepare_candidate(handoff(), plan_id="plan-1", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=True)
    class Candidate:
        def __init__(self, **fields):
            pass
        def validate(self):
            pass
    class Intake:
        def receive(self, token, value):
            return receipt
    result = submit_candidate(candidate, candidate_type=Candidate, intake=Intake(),
                              token_supplier=lambda: "fixture-token")
    assert result.state == "UNKNOWN" and result.revision is None
