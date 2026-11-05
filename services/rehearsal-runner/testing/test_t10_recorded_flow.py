"""Real T10 admission → T15 signed record → T10 intake → change approval.

Kubernetes, T11 and the bucket Job are fixtures here; `rke2_rehearsal_flow.py`
runs the same host path against a cluster. Run from services/platform-events.
"""

import json
import time
from dataclasses import replace

import jwt
import pytest
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.change_intake import Candidate, ChangeIntake
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.rehearsal import RehearsalService
from bravo_platform_events.store import EventError
from bravo_platform_events.workload_identity import WorkloadSender
from cryptography.hazmat.primitives.asymmetric import rsa

from rehearsal_runner.approval_handoff import submit_candidate
from rehearsal_runner.attempt_store import (
    AttemptStore,
    PlanBinding,
    RecordedAttemptSender,
    RecordedEvidence,
)
from rehearsal_runner.bucket_job import BucketEvidence
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.isolation_gate import IsolationBlocked, IsolationPass
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.retry import AttemptPorts
from rehearsal_runner.runner import Namespace, ProposalRef

SPEC = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
TASK = prepare_bucket_policy_task("catalog", '{"Statement":[]}', "fixture-bucket", source_sha="1" * 40)
OPERATOR = Principal("operator", SPEC.tenant_id)


class Catalog:
    def for_plan(self, connection, plan, task_id):
        return TASK

    def for_admission(self, admission):
        return TASK


class Ports:
    """One attempt's fake cluster; verdicts come from the fixture Job."""

    def __init__(self, attempt, verdict, events, *, block=False):
        self.attempt, self.verdict, self.events, self.block = attempt, verdict, events, block

    def propose(self, task):
        self.run_id = task.run_id
        return ProposalRef(task.run_id, task.source_sha, str(self.attempt) * 64)

    def tool_events(self):
        return ({"run_id": self.run_id, "attempt": self.attempt, "tool": "rehearsal.exec",
                 "outcome": "DENIED", "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"},)

    def create(self, run_id):
        self.events.append(("create", run_id, self.attempt))
        return Namespace(f"rehearsal-{self.attempt:024d}", f"uid-{self.attempt}")

    def check(self, namespace):
        if self.block:
            raise IsolationBlocked("fixture")
        return IsolationPass(namespace.name, namespace.uid, f"probe-{self.attempt}")

    def verify_isolated(self, proposal, namespace, isolation):
        self.events.append(("verify", self.attempt))
        return BucketEvidence(self.verdict, "NONE" if self.verdict == "PASS" else "ANONYMOUS_READ_ALLOWED",
                              namespace.name, namespace.uid, proposal.proposal_digest,
                              "sha256:" + "d" * 64, "sha256:" + "e" * 64, {}, {}, b"--- a\n+++ b\n")

    def cleanup(self, namespace):
        self.events.append(("cleanup", namespace.uid))
        return True


class Planner:
    def __init__(self):
        self.verdicts, self.events, self.feedback, self.block = ["PASS"], [], [], False

    def binding(self, admission):
        return PlanBinding(SPEC.plan_digest, SPEC.target_environment_id)

    def factory(self, admission, task):
        planner = self

        class Factory:
            def prepare(self, attempt, feedback_ref):
                planner.feedback.append(feedback_ref)
                verdict = planner.verdicts[attempt - 1]
                ports = Ports(attempt, verdict, planner.events, block=planner.block)
                return AttemptPorts(ports, ports, ports, ports)

        return Factory()


@pytest.fixture
def flow(tmp_path):
    store = PlanStore(tmp_path / "t10.db")
    store.register_plan(SPEC, run_id="fixture", occurred_at="2026-11-05T00:00:00+00:00")
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
    records = AttemptStore(tmp_path / "records", b"r" * 32)
    planner = Planner()
    evidence = RecordedEvidence(records)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode({"iss": "https://issuer.invalid", "aud": "t10", "sub": "t15-runner",
                        "iat": now, "exp": now + 300}, key, algorithm="RS256")
    workload = WorkloadSender("https://issuer.invalid", "t10", "t15-runner", key.public_key(), SPEC.tenant_id)
    intake = ChangeIntake(store, sender=workload, read_original=evidence.read_original,
                          authorize_hold=lambda _c, who, *_: who == "security")
    delivered = []

    def deliver(submission):
        delivered.append(submit_candidate(submission, candidate_type=Candidate, intake=intake,
                                          token_supplier=lambda: token))

    sender = RecordedAttemptSender(records, planner, deliver=deliver)
    service = RehearsalService(store, host=T15RehearsalHost(Catalog(), sender, evidence),
                               check_start=lambda *_: None)
    approvals = ChangeApprovalService(store, authority=intake.current)
    return dict(store=store, records=records, planner=planner, sender=sender, evidence=evidence,
                service=service, token=token, intake=intake, approvals=approvals, path=tmp_path / "records",
                delivered=delivered)


def start(flow, attempt=1, key="start-1"):
    return flow["service"].start(OPERATOR, SPEC.plan_id, task_id="bucket", expected_version=1,
                                 attempt=attempt, command_key=key)


def reconcile(flow, receipt):
    return flow["service"].reconcile(OPERATOR, SPEC.plan_id, rehearsal_id=receipt["rehearsal_id"],
                                     attempt=receipt["attempt"])


def test_passing_rehearsal_reaches_change_approval_with_bound_diff_and_digest(flow):
    receipt = start(flow)
    [delivery] = flow["delivered"]
    assert delivery.state == "ACKNOWLEDGED"
    result = reconcile(flow, receipt)
    assert result["status"] == "PASS" and result["verification_id"] == delivery.verification_id
    submission = flow["evidence"].submission(SPEC.plan_id, receipt["rehearsal_id"], result["verification_id"])
    with flow["store"]._connection() as connection:
        current = flow["intake"].current(connection, OPERATOR, {"plan_id": SPEC.plan_id, "version": 1},
                                         rehearsal_id=receipt["rehearsal_id"])
    assert current.binding["diff_hash"] == submission.diff_hash == "sha256:" + "d" * 64
    assert current.binding["image_digest"] == "sha256:" + "e" * 64
    approval = flow["approvals"].approve(OPERATOR, SPEC.plan_id, run_id=receipt["rehearsal_id"],
                                         binding=current.binding, expected_version=1, command_key="approve-1")
    assert approval["binding"] == current.binding
    with pytest.raises(EventError, match="binding_mismatch"):
        flow["approvals"].approve(OPERATOR, SPEC.plan_id, run_id=receipt["rehearsal_id"],
                                  binding=current.binding | {"image_digest": "sha256:" + "0" * 64},
                                  expected_version=1, command_key="approve-2")


def test_failed_attempt_retries_only_after_hold_with_its_evidence(flow):
    flow["planner"].verdicts = ["FAIL", "PASS"]
    first = start(flow)
    failed = reconcile(flow, first)
    assert failed["status"] == "FAIL"
    with pytest.raises(EventError, match="rehearsal_retry_unavailable"):
        start(flow, 2, "start-2")
    flow["intake"].hold("security", SPEC.plan_id, failed["verification_id"], decision_ref="retry-1")
    second = start(flow, 2, "start-2")
    assert second["rehearsal_id"] == first["rehearsal_id"]
    record = flow["records"].read(first["rehearsal_id"], 1)
    assert flow["planner"].feedback == [None, record["evidence_digest"]]
    assert reconcile(flow, second)["status"] == "PASS"
    assert [d.attempt for d in flow["delivered"]] == [2]


def test_claimed_attempt_without_result_is_unconfirmed_and_never_rerun(flow):
    lost = replace(flow["sender"], dispatch=lambda work: None)
    flow["service"].host = T15RehearsalHost(Catalog(), lost, flow["evidence"])
    receipt = start(flow)
    assert reconcile(flow, receipt)["status"] == "UNCONFIRMED"
    assert flow["records"].claimed(receipt["rehearsal_id"], 1)
    flow["service"].host = T15RehearsalHost(Catalog(), flow["sender"], flow["evidence"])
    flow["service"].host.submit(_admission(flow, receipt))
    assert flow["planner"].events == []


def test_blocked_isolation_needs_a_human_and_stays_unconfirmed(flow):
    flow["planner"].block = True
    receipt = start(flow)
    assert reconcile(flow, receipt)["status"] == "UNCONFIRMED"
    record = flow["records"].read(receipt["rehearsal_id"], 1)
    assert (record["verdict"], record["reason"]) == ("NEEDS_HUMAN", "ISOLATION_BLOCKED")
    assert ("verify", 1) not in flow["planner"].events


def test_undelivered_pass_is_not_confirmed(flow):
    flow["service"].host = T15RehearsalHost(Catalog(), replace(flow["sender"], deliver=None), flow["evidence"])
    receipt = start(flow)
    with pytest.raises(EventError, match="rehearsal_verification_unavailable"):
        reconcile(flow, receipt)


def test_signed_record_carries_tool_events_for_t10(flow):
    flow["planner"].verdicts = ["FAIL", "PASS"]
    first = start(flow)
    failed = reconcile(flow, first)
    flow["intake"].hold("security", SPEC.plan_id, failed["verification_id"], decision_ref="retry-1")
    second = start(flow, 2, "start-2")
    for receipt in (first, second):
        from bravo_platform_events.rehearsal import admitted_attempt

        with flow["store"]._connection() as connection:
            admission = admitted_attempt(connection, SPEC.plan_id, receipt["rehearsal_id"], receipt["attempt"])
        events = flow["evidence"].tool_events(admission)
        assert events == [{"run_id": receipt["rehearsal_id"], "attempt": receipt["attempt"],
                           "tool": "rehearsal.exec", "outcome": "DENIED",
                           "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}]


def test_altered_record_is_not_reconciled(flow):
    flow["planner"].verdicts = ["FAIL"]
    receipt = start(flow)
    path = flow["path"] / receipt["rehearsal_id"] / "1" / "result.json"
    raw = json.loads(path.read_text())
    raw["record"]["verdict"] = "PASS"
    path.write_text(json.dumps(raw))
    with pytest.raises(EventError, match="authority_unavailable"):
        reconcile(flow, receipt)


def _admission(flow, receipt):
    from bravo_platform_events.rehearsal import admitted_attempt

    with flow["store"]._connection() as connection:
        return admitted_attempt(connection, SPEC.plan_id, receipt["rehearsal_id"], 1)
