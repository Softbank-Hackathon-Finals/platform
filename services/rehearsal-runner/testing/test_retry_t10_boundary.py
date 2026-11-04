"""A local assessor PASS cannot substitute for T10's production authority."""

from types import SimpleNamespace

import pytest
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.store import EventError

from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.retry import AttemptPorts, RetryDecision, run_retries
from rehearsal_runner.runner import Namespace, ProposalRef


class Ports:
    def prepare(self, attempt, feedback_ref):
        return AttemptPorts(self, self, self, self)

    def propose(self, task):
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)

    def create(self, run_id):
        return Namespace("rehearsal-test", "uid-1")

    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")

    def verify_isolated(self, proposal, namespace, isolation):
        return {"verdict": "PASS"}

    def cleanup(self, namespace):
        return True

    def assess(self, receipt, proposal, attempt):
        return RetryDecision(proposal.run_id, proposal.source_sha, proposal.proposal_digest, attempt, "PASS")


def test_synthetic_retry_success_cannot_supply_change_authority(tmp_path):
    ports = Ports()
    result = run_retries(SimpleNamespace(run_id="run-1", source_sha="1" * 40), ports, ports)
    assert result.state == "RECEIPT_UNVERIFIED"
    now = "2026-11-03T10:00:00+00:00"
    spec = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    binding = dict(plan_id=spec.plan_id, plan_digest=spec.plan_digest, diff_hash="sha256:" + "c" * 64,
                   image_digest="sha256:" + "d" * 64, target_environment_id="env-onprem",
                   expires_at="2026-11-03T10:10:00+00:00")
    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(spec, run_id="fixture", occurred_at=now)
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    service = ChangeApprovalService(store, authority=lambda *args, **kwargs: result, clock=lambda: now)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(Principal("operator", "tenant-demo"), spec.plan_id, run_id="run-1",
                        binding=binding, expected_version=1, command_key="approval-1")


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
