"""Real T10 transactions and T38 validation; synthetic source/auth only."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest, digest
from rehearsal_runner.admission import logical_task_digest
from test_cutover import EXPIRES, NOW
from test_t10_recorded_flow import OPERATOR, SPEC, TASK

from bravo_platform_events.product_inputs import PURPOSE, ProductInputRecords
from bravo_platform_events.rehearsal import RehearsalService, RehearsalTask
from bravo_platform_events.store import EventError

PRINCIPAL = "d" * 64


class Fixture:
    def __init__(self, backend):
        self.backend, self.now, self.allowed = backend, NOW, True
        self.task = TASK
        self.parts_override = None
        self.calls = []
        backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
        with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
            c.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (SPEC.plan_id,))
        host = SimpleNamespace(task=lambda *_: RehearsalTask(TASK.source_sha, logical_task_digest(TASK)),
                               submit=self.calls.append, observe=lambda *_: None)
        service = RehearsalService(backend.store, host=host, check_start=lambda *_: None)
        service.start(OPERATOR, SPEC.plan_id, task_id="bucket", expected_version=1,
                      attempt=1, command_key="start")
        self.admission, = self.calls
        self.executing_task = replace(TASK, run_id=self.admission.rehearsal_id)

    def for_plan(self, c, plan, task_id):
        assert plan["plan_id"] == SPEC.plan_id and task_id == "bucket"
        return self.task

    def authorize(self, c, principal, plan, admission, purpose):
        assert purpose == PURPOSE
        if not self.allowed or principal != PRINCIPAL or plan["tenant_id"] != SPEC.tenant_id:
            raise ValueError("private grant diagnostic")

    def source(self, c, plan, admission, task):
        if self.parts_override is not None:
            return self.parts_override
        content = task.source_policy
        return dict(bundle=FailureBundle(run_id=task.run_id, source_sha=task.source_sha,
            failure_code="POLICY_TRANSLATION", summary="Fixture bucket policy", evidence_digest="0" * 64),
            manifest=SnapshotManifest(source_sha=task.source_sha,
                files={"policy.json": hashlib.sha256(content.encode()).hexdigest()},
                editable_paths=["policy.json"]), files={"policy.json": content})

    def records(self):
        return ProductInputRecords(self.backend.reopen(), catalog=self, source=self.source,
                                   authorize=self.authorize, clock=lambda: self.now)

    def value(self):
        return self.records().product_input(self.admission, self.executing_task)

    def register(self, value=None, **changes):
        return self.records().register(self.admission, self.value() if value is None else value,
            **(dict(principal=PRINCIPAL, expires_at=EXPIRES) | changes))

    def read(self, ref, principal=PRINCIPAL):
        return self.records().read(principal, ref["input_ref"])


@pytest.fixture
def fixture(backend):
    return Fixture(backend)


def test_commit_reopen_and_concurrent_replay_keep_one_detached_input(fixture):
    f = fixture
    value = f.value()
    with ThreadPoolExecutor(max_workers=3) as pool:
        refs = list(pool.map(lambda _: f.register(value), range(3)))
    assert refs == [refs[0]] * 3
    record = f.read(refs[0])
    assert set(record) == {"admission", "product_input", "input_digest", "principal", "purpose", "expires_at"}
    assert record["input_digest"] == digest(value)
    assert record["product_input"]["admission_digest"] == digest(record["admission"])
    assert record["admission"]["task_digest"] != digest(value.bundle)
    record["product_input"]["files"]["policy.json"] = "changed"
    assert f.read(refs[0])["product_input"]["files"] == value.files
    assert f.calls == [f.admission]  # No submission or model call on replay.


@pytest.mark.parametrize("change", [dict(tenant_id="foreign"), dict(plan_id="missing"),
    dict(plan_version=2), dict(task_id="other"), dict(rehearsal_id="uncommitted"),
    dict(attempt=2), dict(attempt=True), dict(source_sha="e" * 40), dict(task_digest="f" * 64)])
def test_caller_admission_cannot_create_authority(fixture, change):
    f = fixture
    forged = replace(f.admission, **change)
    with pytest.raises(EventError):
        f.records().product_input(forged, replace(f.executing_task, run_id=forged.rehearsal_id))


@pytest.mark.parametrize("field,value", [("state", "GATE"), ("version", 2), ("tenant_id", "foreign")])
def test_current_plan_changes_reject_stored_input(fixture, field, value):
    f = fixture
    ref = f.register()
    with f.backend.admin() as c:
        placeholder = "%s" if f.backend.cluster else "?"
        c.execute(f"UPDATE plans SET {field}={placeholder} WHERE plan_id={placeholder}", (value, SPEC.plan_id))
    with pytest.raises(EventError, match="product_admission_unavailable"):
        f.read(ref)


def test_catalog_registration_and_current_permission_remain_required(fixture):
    f = fixture
    ref = f.register()
    f.task = replace(TASK, source_sha="f" * 40)
    with pytest.raises(EventError, match="product_source_unavailable"):
        f.read(ref)
    f.task = TASK
    f.allowed = False
    with pytest.raises(EventError, match="^product_input_unavailable$"):
        f.read(ref)
    f.allowed = True
    with pytest.raises(EventError, match="product_input_unavailable"):
        f.read(ref, "e" * 64)
    assert f.read(ref)["principal"] == PRINCIPAL


def test_expiry_revocation_and_changed_registration_never_issue_fresh_reference(fixture):
    f = fixture
    ref = f.register()
    with pytest.raises(EventError, match="product_input_conflict"):
        f.register(expires_at="2026-11-03T10:11:00Z")
    f.now = EXPIRES
    with pytest.raises(EventError, match="product_input_unavailable"):
        f.read(ref)
    f.now = NOW
    receipt = f.records().revoke(ref["input_ref"], administration_ref="management-withdrawal")
    assert f.records().revoke(ref["input_ref"], administration_ref="management-withdrawal") == receipt
    for op in (lambda: f.read(ref), f.register):
        with pytest.raises(EventError, match="product_input_unavailable"):
            op()


def test_only_trusted_source_can_register_and_failed_write_leaves_no_reference(fixture):
    f = fixture
    value = f.value()
    forged = value.model_copy(update={"bundle": value.bundle.model_copy(update={"summary": "other objective"})})
    with pytest.raises(EventError, match="product_input_mismatch"):
        f.register(forged)
    with f.backend.reject_insert("plan_commands"), pytest.raises(f.backend.storage_error):
        f.register(value)
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("product-%",)).fetchone()["n"] == 0
    ref = f.register(value)
    assert f.read(ref)["input_digest"] == digest(value)


def test_invalid_source_bytes_fail_before_registration_and_do_not_leak(fixture):
    f = fixture
    f.parts_override = f.source(None, None, f.admission, f.executing_task)
    f.parts_override["files"]["policy.json"] = "changed without manifest update"
    with pytest.raises(EventError, match="^product_source_unavailable$"):
        f.value()
    with pytest.raises(EventError):
        f.records().read(PRINCIPAL, "unknown-reference")


def test_expiry_during_source_read_prevents_commit(fixture):
    f = fixture
    value = f.value()
    original = f.source
    def delayed(*args):
        result = original(*args)
        f.now = EXPIRES
        return result
    f.source = delayed
    with pytest.raises(EventError, match="product_input_unavailable"):
        f.register(value)


def test_real_t38_resolver_consumes_registered_input_and_current_revocation(fixture):
    from bravo_agent.subscription_product_host import RegisteredProductInputs

    f = fixture
    ref = f.register()
    resolver = RegisteredProductInputs(f.records().read, clock=lambda: datetime.fromisoformat(f.now))
    assert resolver.resolve(PRINCIPAL, ref["input_ref"]) == f.value()
    f.records().revoke(ref["input_ref"], administration_ref="management-withdrawal")
    with pytest.raises(ValueError, match="^registered product input unavailable$"):
        resolver.resolve(PRINCIPAL, ref["input_ref"])
