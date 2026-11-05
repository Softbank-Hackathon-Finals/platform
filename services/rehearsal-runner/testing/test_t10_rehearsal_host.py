"""Actual T10 RehearsalService with T15 host; external dependencies are fixtures."""

from dataclasses import replace

import pytest
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.rehearsal import Admission, Observation, RehearsalService
from bravo_platform_events.store import EventError

from rehearsal_runner.admission import logical_task_digest, run_admitted_attempt
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.retry import AttemptPorts
from rehearsal_runner.runner import Namespace, ProposalRef


class Catalog:
    def __init__(self):
        self.source = prepare_bucket_policy_task("catalog", '{"Statement":[]}', "fixture-bucket", source_sha="a" * 40)
        self.after_admission = self.source

    def for_plan(self, connection, plan, task_id):
        if task_id != "bucket":
            raise ValueError("unknown task")
        return self.source

    def for_admission(self, admission):
        return self.after_admission


class Ports:
    def __init__(self):
        self.attempt = 1
        self.events = []

    def prepare(self, attempt, feedback_ref):
        self.attempt = attempt
        self.events.append(("prepare", attempt))
        return AttemptPorts(self, self, self, self)

    def propose(self, task):
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


class Sender:
    def __init__(self):
        self.ports = Ports()
        self.calls = []

    def submit_once(self, admission, task):
        self.calls.append(admission)
        result = run_admitted_attempt(admission, task, self.ports)
        assert result.state == "RECEIPT_UNVERIFIED"


class Evidence:
    def __init__(self):
        self.result = None

    def read_authenticated(self, admission):
        return self.result


@pytest.fixture
def environment(tmp_path):
    spec = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(spec, run_id="fixture", occurred_at="2026-11-03T10:00:00+00:00")
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    catalog, sender, evidence = Catalog(), Sender(), Evidence()
    host = T15RehearsalHost(catalog, sender, evidence)
    service = RehearsalService(store, host=host, check_start=lambda *_: None)
    return spec, catalog, sender, evidence, host, service


def test_t10_commits_then_t15_host_submits_one_bound_attempt(environment):
    spec, _, sender, _, _, service = environment
    principal = Principal("operator", spec.tenant_id)
    args = dict(task_id="bucket", expected_version=1, attempt=1, command_key="first")
    receipt = service.start(principal, spec.plan_id, **args)
    assert service.start(principal, spec.plan_id, **(args | {"command_key": "second"})) == receipt
    assert len(sender.calls) == 1
    assert sender.ports.events == [("prepare", 1), ("create", receipt["rehearsal_id"]),
                                   ("verify", 1), ("cleanup", "uid-1")]
    result = service.reconcile(principal, spec.plan_id, rehearsal_id=receipt["rehearsal_id"], attempt=1)
    assert result["status"] == "UNCONFIRMED"


def test_changed_catalog_after_commit_cannot_be_submitted(environment):
    spec, catalog, sender, _, _, service = environment
    catalog.after_admission = prepare_bucket_policy_task("catalog", '{"Statement":[]}', "other-bucket", source_sha="a" * 40)
    with pytest.raises(EventError, match="rehearsal_submission_unknown"):
        service.start(Principal("operator", spec.tenant_id), spec.plan_id, task_id="bucket",
                      expected_version=1, attempt=1, command_key="first")
    assert sender.calls == []


def test_host_does_not_treat_wrong_or_unconfirmed_evidence_as_observation(environment):
    spec, catalog, _, evidence, host, _ = environment
    task = catalog.source
    admission = Admission(spec.plan_id, spec.tenant_id, 1, "bucket", "run-1", 1,
                          task.source_sha, logical_task_digest(task))
    assert host.observe(admission) is None
    original = Observation(admission, "verification-1", "c" * 64, "rehearsal-fixture", "uid-1", "uid-1", True, "PASS")
    evidence.result = replace(original, removed_uid="uid-other")
    with pytest.raises(ValueError, match="unbound"):
        host.observe(admission)
    evidence.result = replace(original, admission=replace(admission, attempt=2))
    with pytest.raises(ValueError, match="unbound"):
        host.observe(admission)
    evidence.result = original
    assert host.observe(admission) == original
