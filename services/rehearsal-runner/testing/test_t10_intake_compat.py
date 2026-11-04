"""Cross-check T15 candidates against T10 PR #237; fixture evidence only."""

import hashlib
import time

import jwt
import pytest
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.change_intake import Candidate, ChangeIntake, SourceVerification
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.store import EventError
from bravo_platform_events.workload_identity import WorkloadSender
from cryptography.hazmat.primitives.asymmetric import rsa

from rehearsal_runner.approval_handoff import prepare_candidate
from rehearsal_runner.integration_handoff import prepare_handoff
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef

NOW = "2026-11-03T10:00:00+00:00"
EXPIRES = "2026-11-03T10:10:00+00:00"
SPEC = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)


def submitted_candidate():
    namespace = Namespace("rehearsal-fixture", "fixture-uid-1")
    handoff = prepare_handoff(ProposalRef("run-fixture", "1" * 40, "b" * 64), namespace,
                              IsolationPass(namespace.name, namespace.uid, "probe-1"), attempt=1,
                              diff=b"fixture diff", image_digest="sha256:" + "d" * 64,
                              target_environment_id="env-onprem")
    submission = prepare_candidate(handoff, plan_id=SPEC.plan_id, verification_id="verification-fixture-1",
                                   evidence_ref="evidence-fixture-1", cleanup_uid="fixture-uid-1",
                                   cleanup_confirmed=True)
    return Candidate(**submission.t10_fields())


@pytest.fixture
def fixture(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode({"iss": "https://issuer.invalid", "aud": "t10-fixture", "sub": "fixture-sa",
                        "iat": now, "exp": now + 120}, key, algorithm="RS256")
    sender = WorkloadSender("https://issuer.invalid", "t10-fixture", "fixture-sa", key.public_key(), SPEC.tenant_id)
    store = PlanStore(tmp_path / "t10.db")
    store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
    # This fixture is built independently of the submitted candidate.
    original_candidate = Candidate(SPEC.plan_id, "run-fixture", 1, "verification-fixture-1", "evidence-fixture-1",
                                   "env-onprem", "rehearsal-fixture", "b" * 64,
                                   "sha256:" + hashlib.sha256(b"fixture diff").hexdigest(), "sha256:" + "d" * 64)
    original = SourceVerification(original_candidate, SPEC.tenant_id, SPEC.plan_digest, 1, EXPIRES,
                                  "PASS", "fixture-uid-1", "fixture-uid-1", True)
    reads = []
    def read(*refs):
        reads.append(refs)
        return original
    intake = ChangeIntake(store, sender=sender, read_original=read,
                          authorize_hold=lambda _c, who, plan, verification, ref: who == "authorized-fixture",
                          clock=lambda: NOW)
    return intake, token, store, reads


def test_real_t10_intake_accepts_matching_candidate_then_hold_blocks_replay(fixture):
    intake, token, store, reads = fixture
    candidate = submitted_candidate()
    receipt = intake.receive(token, candidate)
    assert receipt == intake.receive(token, candidate)
    assert reads == [(SPEC.plan_id, candidate.run_id, candidate.verification_id, candidate.evidence_ref)] * 2
    intake.hold("authorized-fixture", SPEC.plan_id, candidate.verification_id, decision_ref="decision-1")
    with pytest.raises(EventError, match="verification_held"):
        intake.receive(token, candidate)
    binding = dict(plan_id=SPEC.plan_id, plan_digest=SPEC.plan_digest, diff_hash=candidate.diff_hash,
                   image_digest=candidate.image_digest, target_environment_id=candidate.target_environment_id,
                   expires_at=EXPIRES)
    approval = ChangeApprovalService(store, authority=intake.current, clock=lambda: NOW)
    with pytest.raises(EventError, match="authority_unavailable"):
        approval.approve(Principal("operator", SPEC.tenant_id), SPEC.plan_id, run_id=candidate.run_id,
                         binding=binding, expected_version=1, command_key="approval-1")


def test_t10_rejects_sender_before_reading_original(fixture):
    intake, _, _, reads = fixture
    with pytest.raises(EventError, match="forbidden"):
        intake.receive("bad-token", submitted_candidate())
    assert reads == []


def test_t10_rejects_different_submitted_image(fixture):
    from dataclasses import replace

    intake, token, _, _ = fixture
    with pytest.raises(EventError, match="authority_unavailable"):
        intake.receive(token, replace(submitted_candidate(), image_digest="sha256:" + "e" * 64))


def test_t15_submit_adapter_calls_real_t10_intake_and_hold_blocks_it(fixture):
    from rehearsal_runner.approval_handoff import submit_candidate

    intake, token, _, _ = fixture
    namespace = Namespace("rehearsal-fixture", "fixture-uid-1")
    handoff = prepare_handoff(ProposalRef("run-fixture", "1" * 40, "b" * 64), namespace,
                              IsolationPass(namespace.name, namespace.uid, "probe-1"), attempt=1,
                              diff=b"fixture diff", image_digest="sha256:" + "d" * 64,
                              target_environment_id="env-onprem")
    submission = prepare_candidate(handoff, plan_id=SPEC.plan_id, verification_id="verification-fixture-1",
                                   evidence_ref="evidence-fixture-1", cleanup_uid="fixture-uid-1",
                                   cleanup_confirmed=True)
    result = submit_candidate(submission, candidate_type=Candidate, intake=intake, token_supplier=lambda: token)
    assert result.state == "ACKNOWLEDGED" and len(result.revision) == 64
    assert submit_candidate(submission, candidate_type=Candidate, intake=intake,
                            token_supplier=lambda: token) == result
    intake.hold("authorized-fixture", SPEC.plan_id, submission.verification_id, decision_ref="decision-1")
    held = submit_candidate(submission, candidate_type=Candidate, intake=intake, token_supplier=lambda: token)
    assert held.state == "UNKNOWN" and held.revision is None


def test_t15_hold_adapter_confirms_real_t10_hold_before_retry(fixture):
    from rehearsal_runner.approval_handoff import T10PreviousHold

    intake, token, _, _ = fixture
    candidate = submitted_candidate()
    hold = T10PreviousHold(SPEC.plan_id, candidate.run_id, "decision-1", intake, lambda: "authorized-fixture")
    assert hold.confirm(candidate.run_id, candidate.verification_id)
    # T10 persists a hold even if the verification has not arrived yet.
    with pytest.raises(EventError, match="verification_held"):
        intake.receive(token, candidate)


def test_unauthorized_t15_hold_cannot_be_confirmed(fixture):
    from rehearsal_runner.approval_handoff import T10PreviousHold

    intake, _, _, _ = fixture
    hold = T10PreviousHold(SPEC.plan_id, "run-fixture", "decision-1", intake, lambda: "wrong-fixture")
    assert not hold.confirm("run-fixture", "verification-fixture-1")
