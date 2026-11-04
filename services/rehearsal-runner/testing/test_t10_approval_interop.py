"""Local T15/T10 boundary: a successful observation is not approval proof."""

import pytest
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.store import EventError

from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.bucket_verifier import BucketVerifier
from rehearsal_runner.credential_verifier import CredentialVerifier
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once
from rehearsal_runner.secret_task import prepare_credential_migration

NOW = "2026-11-03T10:00:00+00:00"
SPEC = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
BINDING = dict(plan_id=SPEC.plan_id, plan_digest=SPEC.plan_digest, diff_hash="sha256:" + "c" * 64,
               image_digest="sha256:" + "d" * 64, target_environment_id="env-onprem",
               expires_at="2026-11-03T10:10:00+00:00")


class Proposer:
    def propose(self, task):
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)


class Lifecycle:
    def create(self, run_id):
        return Namespace("rehearsal-test", "uid-original")

    def cleanup(self, namespace):
        return True


class Isolation:
    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")


class UntrustedJob:
    def verify(self, request):
        return {"verdict": "PASS"}


@pytest.mark.parametrize("kind", ["bucket", "credentials"])
def test_actual_t15_result_cannot_supply_t10_approval_authority(tmp_path, kind):
    sha = "1" * 40
    if kind == "bucket":
        task = prepare_bucket_policy_task("run-1", '{"Statement": []}', "rehearsal-test", source_sha=sha)
        verifier = BucketVerifier(task, UntrustedJob())
    else:
        task = prepare_credential_migration("run-1", sha)
        verifier = CredentialVerifier(task, UntrustedJob())
    result = run_once(task, Proposer(), Lifecycle(), Isolation(), verifier)
    assert result.state == "RECEIPT_UNVERIFIED"

    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
    service = ChangeApprovalService(store, authority=lambda *args, **kwargs: result, clock=lambda: NOW)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(Principal("operator", "tenant-demo"), SPEC.plan_id, run_id="run-1",
                        binding=BINDING, expected_version=1, command_key="approval-1")
    assert [event["event_type"] for event in store.read_after(SPEC.plan_id, None)] == ["state.changed"]
