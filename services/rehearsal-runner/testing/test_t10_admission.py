"""Real T10 admission into T15's one-attempt runner; verifier ports are fixtures."""

from dataclasses import replace

import pytest
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.rehearsal import Admission, RehearsalService, RehearsalTask
from bravo_platform_events.store import EventError

from rehearsal_runner.admission import logical_task_digest, run_admitted_attempt
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.retry import AttemptPorts
from rehearsal_runner.runner import Namespace, ProposalRef


class Ports:
    def __init__(self):
        self.events = []
        self.attempt = 0

    def prepare(self, attempt, feedback_ref):
        self.events.append(("prepare", attempt, feedback_ref))
        self.attempt = attempt
        return AttemptPorts(self, self, self, self)

    def propose(self, task):
        self.events.append(("propose", task.run_id))
        return ProposalRef(task.run_id, task.source_sha, "c" * 64)

    def create(self, run_id):
        self.events.append(("create", run_id))
        return Namespace("rehearsal-fixture", "uid-1")

    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")

    def verify_isolated(self, proposal, namespace, isolation):
        self.events.append(("verify", self.attempt))
        return {"fixture": "untrusted"}

    def cleanup(self, namespace):
        self.events.append(("cleanup", namespace.uid))
        return True


def bucket(run_id="placeholder"):
    return prepare_bucket_policy_task(run_id, '{"Statement":[]}', "fixture-bucket", source_sha="a" * 40)


class Host:
    def __init__(self):
        self.ports = Ports()
        self.results = []

    def task(self, connection, plan, task_id):
        assert task_id == "bucket"
        task = bucket()
        return RehearsalTask(task.source_sha, logical_task_digest(task))

    def submit(self, admission):
        self.results.append(run_admitted_attempt(admission, bucket(admission.rehearsal_id), self.ports))

    def observe(self, admission):
        return None


def test_committed_t10_admission_runs_t15_once_and_replay_does_not_resubmit(tmp_path):
    spec = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(spec, run_id="fixture", occurred_at="2026-11-03T10:00:00+00:00")
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    host = Host()
    service = RehearsalService(store, host=host, check_start=lambda *_: None)
    principal = Principal("operator", spec.tenant_id)
    args = dict(task_id="bucket", expected_version=1, attempt=1, command_key="first")
    receipt = service.start(principal, spec.plan_id, **args)
    assert service.start(principal, spec.plan_id, **(args | {"command_key": "second"})) == receipt
    assert len(host.results) == 1 and host.results[0].state == "RECEIPT_UNVERIFIED"
    assert host.ports.events == [("prepare", 1, None), ("propose", receipt["rehearsal_id"]),
                                 ("create", receipt["rehearsal_id"]), ("verify", 1), ("cleanup", "uid-1")]
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='GATE' WHERE plan_id=?", (spec.plan_id,))
    with pytest.raises(EventError, match="rehearsal_not_allowed"):
        service.start(principal, spec.plan_id, **(args | {"task_id": "another", "command_key": "third"}))
    assert len(host.results) == 1


def test_changed_task_or_wrong_attempt_fails_before_any_effect():
    task = bucket("run-1")
    admitted = Admission("plan-1", "tenant-1", 1, "bucket", "run-1", 1, task.source_sha,
                         logical_task_digest(task))
    ports = Ports()
    changed = prepare_bucket_policy_task("run-1", '{"Statement":[]}', "other-bucket", source_sha="a" * 40)
    assert run_admitted_attempt(admitted, changed, ports).reason == "ADMISSION_MISMATCH"
    assert run_admitted_attempt(replace(admitted, attempt=2), task, ports).reason == "FEEDBACK_MISMATCH"
    assert ports.events == []


def test_committed_second_attempt_is_not_restarted_as_first():
    task = bucket("run-1")
    admitted = Admission("plan-1", "tenant-1", 1, "bucket", "run-1", 2, task.source_sha,
                         logical_task_digest(task))
    ports = Ports()
    result = run_admitted_attempt(admitted, task, ports, feedback_digest="d" * 64)
    assert result.state == "RECEIPT_UNVERIFIED"
    assert ("prepare", 2, "d" * 64) in ports.events and ("verify", 2) in ports.events
