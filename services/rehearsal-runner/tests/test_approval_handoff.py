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
