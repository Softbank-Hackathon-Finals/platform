"""Actual T15/T10 boundary; synthetic Job output cannot authorize a change."""

import pytest
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.store import EventError

from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.bucket_verifier import BucketVerifier
from rehearsal_runner.credential_verifier import CredentialVerifier
from rehearsal_runner.isolation_gate import IsolationBlocked, IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once
from rehearsal_runner.secret_task import prepare_credential_migration

NOW = "2026-11-03T10:00:00+00:00"
SPEC = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
BINDING = dict(plan_id=SPEC.plan_id, plan_digest=SPEC.plan_digest, diff_hash="sha256:" + "c" * 64,
               image_digest="sha256:" + "d" * 64, target_environment_id="env-onprem",
               expires_at="2026-11-03T10:10:00+00:00")


class Ports:
    def __init__(self, failure):
        self.failure = failure
        self.calls = []

    def propose(self, task):
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)

    def create(self, run_id):
        return Namespace("rehearsal-test", "uid-original")

    def check(self, namespace):
        if self.failure == "isolation":
            raise IsolationBlocked("not isolated")
        return IsolationPass(namespace.name, namespace.uid, "probe-1")

    def verify(self, request):
        self.calls.append(request)
        return {"verdict": "PASS"}

    def cleanup(self, namespace):
        return self.failure != "cleanup"


@pytest.mark.parametrize("kind", ["bucket", "credentials"])
@pytest.mark.parametrize("failure,expected,job_count", [
    (None, "RECEIPT_UNVERIFIED", 1),
    ("isolation", "BLOCK", 0),
    ("cleanup", "NEEDS_HUMAN", 1),
])
def test_t15_output_cannot_be_used_as_t10_authority(tmp_path, kind, failure, expected, job_count):
    ports = Ports(failure)
    if kind == "bucket":
        task = prepare_bucket_policy_task("run-1", '{"Statement": []}', "rehearsal-test", source_sha="1" * 40)
        verifier = BucketVerifier(task, ports)
    else:
        task = prepare_credential_migration("run-1", "1" * 40)
        verifier = CredentialVerifier(task, ports)
    result = run_once(task, ports, ports, ports, verifier)
    assert result.state == expected
    assert len(ports.calls) == job_count

    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
    service = ChangeApprovalService(store, authority=lambda *args, **kwargs: result, clock=lambda: NOW)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(Principal("operator", "tenant-demo"), SPEC.plan_id, run_id="run-1",
                        binding=BINDING, expected_version=1, command_key="approval-1")
    assert [event["event_type"] for event in store.read_after(SPEC.plan_id, None)] == ["state.changed"]


def test_t15_candidate_data_is_not_itself_t10_authority(tmp_path):
    from rehearsal_runner.approval_handoff import prepare_candidate
    from rehearsal_runner.integration_handoff import prepare_handoff

    namespace = Namespace("rehearsal-test", "uid-1")
    handoff = prepare_handoff(ProposalRef("run-1", "1" * 40, "b" * 64), namespace,
                              IsolationPass(namespace.name, namespace.uid, "probe-1"), attempt=1,
                              diff=b"fixture diff", image_digest="sha256:" + "d" * 64,
                              target_environment_id="env-onprem")
    candidate = prepare_candidate(handoff, plan_id="plan-demo", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=True)
    now = "2026-11-03T10:00:00+00:00"
    spec = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    binding = dict(plan_id=spec.plan_id, plan_digest=spec.plan_digest, diff_hash=handoff.diff_hash,
                   image_digest=handoff.image_digest, target_environment_id=handoff.target_environment_id,
                   expires_at="2026-11-03T10:10:00+00:00")
    store = PlanStore(tmp_path / "candidate.db")
    store.register_plan(spec, run_id="fixture", occurred_at=now)
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    service = ChangeApprovalService(store, authority=lambda *args, **kwargs: candidate, clock=lambda: now)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(Principal("operator", "tenant-demo"), spec.plan_id, run_id="run-1",
                        binding=binding, expected_version=1, command_key="approval-1")
