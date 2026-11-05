"""Real T10 admission reads tasks from the signed catalog. Run from services/platform-events."""

import pytest
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.rehearsal import RehearsalService
from bravo_platform_events.store import EventError

from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.task_catalog import TaskCatalog

SPEC = PlanSpec("plan-fixture", "tenant-fixture", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
POLICY = '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"x"}]}'


class Sender:
    def __init__(self):
        self.submitted = []

    def submit_once(self, admission, task):
        self.submitted.append((admission, task))


@pytest.fixture
def service(tmp_path):
    store = PlanStore(tmp_path / "t10.db")
    store.register_plan(SPEC, run_id="fixture", occurred_at="2026-11-05T00:00:00+00:00")
    with store._connection() as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
    catalog = TaskCatalog(tmp_path / "catalog", b"c" * 32)
    sender = Sender()
    host = T15RehearsalHost(catalog, sender, evidence=None)
    return RehearsalService(store, host=host, check_start=lambda *_: None), catalog, sender


def start(service, task_id):
    return service.start(Principal("operator", SPEC.tenant_id), SPEC.plan_id, task_id=task_id,
                         expected_version=1, attempt=1, command_key="start-" + task_id)


def test_t10_admits_registered_tasks_and_submits_them_with_its_rehearsal_id(service):
    service, catalog, sender = service
    catalog.register(plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id, plan_digest=SPEC.plan_digest,
                     task_id="bucket", kind="bucket-policy", source_sha="1" * 40, source_policy=POLICY,
                     target_bucket="rehearsal-attachments")
    catalog.register(plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id, plan_digest=SPEC.plan_digest,
                     task_id="credentials", kind="credential-migration", source_sha="1" * 40)
    for task_id in ("bucket", "credentials"):
        receipt = start(service, task_id)
        admission, task = sender.submitted[-1]
        assert task.run_id == receipt["rehearsal_id"] == admission.rehearsal_id
        assert logical_task_digest(task) == admission.task_digest


def test_t10_refuses_a_task_registered_for_another_plan_digest(service):
    service, catalog, sender = service
    catalog.register(plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id, plan_digest="f" * 64,
                     task_id="bucket", kind="bucket-policy", source_sha="1" * 40, source_policy=POLICY,
                     target_bucket="rehearsal-attachments")
    with pytest.raises(EventError, match="authority_unavailable"):
        start(service, "bucket")
    with pytest.raises(EventError, match="authority_unavailable"):
        start(service, "unregistered")
    assert sender.submitted == []
