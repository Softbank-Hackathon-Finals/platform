"""T10 admission plus T15 Kubernetes readback; API results are synthetic."""

import pytest
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.rehearsal import (
    RehearsalService,
    RehearsalTask,
    require_rehearsals_complete,
)
from bravo_platform_events.store import EventError

from rehearsal_runner.recovery_lookup import find_live_jobs, find_live_namespace, recovery_identity


class Cluster:
    def __init__(self):
        self.namespace = None
        self.jobs = []
        self.submissions = 0

    def get_json(self, *args):
        if args[0] == "namespaces":
            return {"items": [] if self.namespace is None else [self.namespace]}
        if args[0] == "namespace":
            return self.namespace
        if args[0] == "jobs":
            return {"items": self.jobs}
        raise AssertionError("unexpected API call")


class Host:
    def __init__(self, cluster):
        self.cluster = cluster
        self.discovered = None

    def task(self, connection, plan, task_id):
        return RehearsalTask("a" * 40, "b" * 64)

    def submit(self, admission):
        self.cluster.submissions += 1
        labels, annotations = recovery_identity(admission.rehearsal_id, admission.attempt)
        namespace = "rehearsal-" + "a" * 24
        self.cluster.namespace = {"metadata": {"name": namespace, "uid": "namespace-uid-1",
                                   "labels": labels | {"app.kubernetes.io/part-of": "bravo-rehearsal"},
                                   "annotations": annotations}}
        self.cluster.jobs = [{"metadata": {"name": "verifier", "uid": "job-uid-1", "namespace": namespace,
                                           "labels": labels, "annotations": annotations}}]
        # Simulate response loss after the remote effect, not a new submission.
        raise TimeoutError("fixture acknowledgement lost")

    def observe(self, admission):
        namespace = find_live_namespace(self.cluster, admission.rehearsal_id, admission.attempt)
        jobs = find_live_jobs(self.cluster, namespace, admission.rehearsal_id, admission.attempt)
        self.discovered = (namespace, jobs)
        # Live labels alone are not authenticated T38 results or cleanup proof.
        return None


def test_t10_restart_finds_existing_t15_namespace_and_job_without_resubmitting(tmp_path):
    spec = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    principal = Principal("operator", spec.tenant_id)
    store = PlanStore(tmp_path / "plan.db")
    store.register_plan(spec, run_id="fixture", occurred_at="2026-11-03T10:00:00+00:00")
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    cluster = Cluster()
    host = Host(cluster)
    service = RehearsalService(store, host=host, check_start=lambda *_: None)
    args = dict(task_id="bucket", expected_version=1, attempt=1, command_key="request-1")
    with pytest.raises(EventError, match="rehearsal_submission_unknown"):
        service.start(principal, spec.plan_id, **args)
    receipt = service.start(principal, spec.plan_id, **args)
    assert service.start(principal, spec.plan_id, **(args | {"command_key": "request-2"})) == receipt
    assert cluster.submissions == 1

    restarted = Host(cluster)
    recovery = RehearsalService(PlanStore(tmp_path / "plan.db"), host=restarted, check_start=lambda *_: None)
    result = recovery.reconcile(principal, spec.plan_id, rehearsal_id=receipt["rehearsal_id"], attempt=1)
    assert result["status"] == "UNCONFIRMED"
    namespace, jobs = restarted.discovered
    assert (namespace.uid, jobs[0].uid) == ("namespace-uid-1", "job-uid-1")
    assert cluster.submissions == 1
    with store._transaction(write=True, plan_id=spec.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (spec.plan_id,)).fetchone())
        with pytest.raises(EventError, match="rehearsal_incomplete"):
            require_rehearsals_complete(connection, plan)
