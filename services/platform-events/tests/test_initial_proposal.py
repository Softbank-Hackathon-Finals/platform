"""Real SQLite/PostgreSQL journals; synthetic collection, model and publisher.

No model, registry, cloud or live state transition is invoked by these tests.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from threading import Event

import pytest
from bravo_agent.contracts import Proposal, digest
from bravo_agent.product_execution_contracts import ExecutionPending
from rehearsal_runner.task_catalog import TaskCatalog
from test_cutover import EXPIRES, NOW, PRINCIPAL, SPEC
from test_workflow_recovery import ANALYSIS, collect, service

from bravo_platform_events import initial_proposal as initial
from bravo_platform_events.cutover import _get
from bravo_platform_events.product_access import ProductInputAccess
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import Evidence, WorkflowRecovery

ACTOR = "a" * 64
SOURCE = "b" * 40
RUNTIME = "c" * 40


class Actions:
    def __init__(self, records):
        self.records = records
        self.submits, self.reads, self.keys = [], [], []
        self.envelope = None
        self.hook = lambda: None

    def recipient(self, request_id):
        self.keys.append(request_id)
        return "d" * 64

    def submit(self, intent):
        self.submits.append(intent)
        value = self.records.read_input(ACTOR, SPEC.plan_id, 3,
            input_ref=intent.input_ref, input_digest=intent.input_digest)
        proposal = Proposal(run_id=value["run_id"], source_sha=SOURCE, risk="low",
            rationale="Synthetic migration proposal", edits=[dict(path="app/config.py",
                before_sha256=value["manifest"]["files"]["app/config.py"], replacement="VALUE = 2\n")])
        self.envelope = dict(authenticated_fixture=True, result=dict(request_id=intent.request_id,
            input_digest=intent.input_digest, runtime_source_sha=RUNTIME,
            execution_receipt_id="fixture-run-artifact", proposal=proposal.model_dump(mode="json"),
            proposal_digest=digest(proposal), schema_valid=True, lint_passed=True))
        self.hook()
        return deepcopy(self.envelope)

    def read(self, intent):
        self.reads.append(intent)
        return deepcopy(self.envelope)

    def validate(self, intent, value, envelope):
        # Synthetic authentication port. Production adapters verify provider provenance.
        if envelope["authenticated_fixture"] is not True:
            raise ValueError("untrusted provider")
        return envelope["result"]


class Fixture:
    def __init__(self, backend, tmp_path):
        self.backend, self.now = backend, NOW
        collect(backend.store)
        recovery = WorkflowRecovery(service(backend.store, None), read_evidence=lambda c, p, *args:
            Evidence(SPEC.plan_id, SPEC.plan_digest, SPEC.target_environment_id, p["version"],
                     "analysis-fixture", ANALYSIS))
        assert recovery.resume_once(PRINCIPAL, SPEC.plan_id) == "PROPOSE"
        self.catalog = TaskCatalog(tmp_path / "catalog", b"fixture-signing-material" * 2)
        self.catalog.register(plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id, plan_digest=SPEC.plan_digest,
                              task_id="credential", kind="credential-migration", source_sha=SOURCE)
        content = "VALUE = 1\n"
        self.source_value = dict(manifest=dict(source_sha=SOURCE,
            files={"app/config.py": hashlib.sha256(content.encode()).hexdigest()},
            editable_paths=["app/config.py"]), files={"app/config.py": content})
        self.access = ProductInputAccess(backend.store, clock=lambda: self.now, purpose=initial.PURPOSE)
        self.grant(True, 0)
        self.records = self.reopen()
        self.ref = self.admit()
        self.client = Actions(self.records)
        self.port = self.execution()
        self.published = None

    def grant(self, enabled, revision):
        return self.access.set_grant(SPEC.plan_id, ACTOR, tenant_id=SPEC.tenant_id, enabled=enabled,
            valid_after=NOW, expires_at=EXPIRES, expected_revision=revision,
            administration_ref="fixture-management", command_key=f"grant-{revision}")

    def reopen(self):
        return initial.InitialProposalRecords(self.backend.reopen(), catalog=self.catalog,
            source=lambda *args: self.source_value, authorize=self.access.require, clock=lambda: self.now)

    def execution(self, **changes):
        return initial.InitialProposalExecution(self.reopen(), self.client,
            **(dict(principal=ACTOR, runtime_source_sha=RUNTIME) | changes))

    def admit(self, **changes):
        return self.records.admit(ACTOR, **(dict(plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id,
            task_id="credential", expected_version=3, expires_at=EXPIRES) | changes))

    def execute(self):
        return self.port.execute(SPEC.plan_id, 3, **self.ref)

    def result_rows(self):
        with self.backend.store._transaction() as c:
            return c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                             ("initial-proposal:%:result",)).fetchone()["n"]

    def publish(self):
        result = self.port.stored(SPEC.plan_id, 3, **self.ref)
        self.published = dict(receipt_id="fixture-published-image", request_id=result["request_id"],
            plan_digest=SPEC.plan_digest, source_sha=SOURCE, proposal_digest=result["proposal_digest"],
            diff_hash="sha256:" + "e" * 64, image_digest="sha256:" + "f" * 64)

    def resume(self):
        reader = self.port.evidence_reader(publication=lambda *args: self.published)
        return WorkflowRecovery(service(self.backend.store, None), read_evidence=reader).resume_once(
            PRINCIPAL, SPEC.plan_id)


@pytest.fixture
def fixture(backend, tmp_path):
    return Fixture(backend, tmp_path)


def test_normal_transition_requires_publication_and_consumes_same_stored_proposal(fixture):
    f = fixture
    result = f.execute()
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "PROPOSE"
    with pytest.raises(EventError, match="initial_publication_unconfirmed"):
        f.resume()
    f.publish()
    assert f.resume() == "REHEARSE"
    assert f.execution().stored(SPEC.plan_id, 3, **f.ref) == result
    assert len(f.client.submits) == 1 and not f.client.reads
    with f.backend.store._transaction() as c:
        proof = _get(c, SPEC.plan_id, "workflow:3")[1]
        assert proof["result"]["change"]["image_digest"] == f.published["image_digest"]
        assert proof["result"]["inventory_digest"] == ANALYSIS.inventory_digest
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("rehearsal:%",)).fetchone()["n"] == 0


def test_concurrent_admission_and_detached_input(fixture):
    f = fixture
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _: f.admit(), range(3))) == [f.ref] * 3
    value = f.records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    assert value["binding"]["stage"] == "PROPOSE"
    assert "admission" not in value and "bundle" not in value
    value["files"]["app/config.py"] = "changed"
    assert f.records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)["files"] != value["files"]


def test_preparation_is_read_only_and_same_durable_intent_survives_restart(fixture):
    f = fixture
    fields = f.port.preparation_fields(SPEC.plan_id, 3, **f.ref)
    assert not f.client.keys and not f.client.submits
    with pytest.raises(EventError, match="not_started"):
        f.port.read(SPEC.plan_id, 3, **f.ref)
    result = f.execute()
    assert f.client.submits[0].model_dump(exclude={"recipient_public_key"}) == fields
    assert f.execution().execute(SPEC.plan_id, 3, **f.ref) == result
    assert len(f.client.submits) == len(f.client.keys) == 1 and not f.client.reads
    with pytest.raises(EventError, match="already_claimed"):
        f.port.preparation_fields(SPEC.plan_id, 3, **f.ref)


def test_lost_response_only_reads_original_request_after_restart(fixture):
    f = fixture
    def loss():
        raise RuntimeError("private upstream failure")
    f.client.hook = loss
    with pytest.raises(EventError, match="^initial_execution_unconfirmed$"):
        f.execute()
    assert f.result_rows() == 0
    assert f.execution().execute(SPEC.plan_id, 3, **f.ref)["schema_valid"] is True
    assert f.client.submits == f.client.reads and len(f.client.submits) == 1


def test_simultaneous_call_does_not_submit_twice(fixture):
    f = fixture
    started, release = Event(), Event()
    def pending():
        started.set()
        assert release.wait(5)
    f.client.hook = pending
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(f.execute)
        assert started.wait(5)
        try:
            second = f.execution().execute(SPEC.plan_id, 3, **f.ref)
        finally:
            release.set()
        assert first.result() == second
    assert len(f.client.submits) == len(f.client.reads) == 1


def test_crash_after_claim_before_submit_does_not_reexecute(fixture, monkeypatch):
    f = fixture
    class Crash(BaseException):
        pass
    def crash(intent):
        raise Crash()
    monkeypatch.setattr(f.client, "submit", crash)
    with pytest.raises(Crash):
        f.execute()
    with pytest.raises(EventError, match="unconfirmed"):
        f.execution().execute(SPEC.plan_id, 3, **f.ref)
    assert not f.client.submits and len(f.client.reads) == 1


def test_revocation_during_execution_prevents_commit_and_later_reads(fixture):
    f = fixture
    f.client.hook = lambda: f.grant(False, 1)
    with pytest.raises(EventError, match="unavailable"):
        f.execute()
    assert f.result_rows() == 0
    with pytest.raises(EventError, match="unavailable"):
        f.port.read(SPEC.plan_id, 3, **f.ref)
    assert len(f.client.submits) == 1 and not f.client.reads


def test_initial_and_rehearsal_grants_are_distinct(fixture):
    f = fixture
    f.grant(False, 1)
    rehearsal = ProductInputAccess(f.backend.store, clock=lambda: NOW)
    rehearsal.set_grant(SPEC.plan_id, ACTOR, tenant_id=SPEC.tenant_id, enabled=True,
        valid_after=NOW, expires_at=EXPIRES, expected_revision=0,
        administration_ref="fixture-management", command_key="grant-0")
    with pytest.raises(EventError, match="unavailable"):
        f.execute()
    assert not f.client.submits


@pytest.mark.parametrize("field,value", [("state", "REHEARSE"), ("version", 9), ("tenant_id", "foreign")])
def test_plan_state_version_tenant_change_rejects_before_dispatch(fixture, field, value):
    f = fixture
    with f.backend.admin() as c:
        placeholder = "%s" if f.backend.cluster else "?"
        c.execute(f"UPDATE plans SET {field}={placeholder} WHERE plan_id={placeholder}", (value, SPEC.plan_id))
    with pytest.raises(EventError, match="unavailable"):
        f.execute()
    assert not f.client.submits


def test_expired_source_change_and_other_actor_fail_before_dispatch(fixture):
    f = fixture
    f.source_value["files"]["app/config.py"] = "VALUE = 7\n"
    with pytest.raises(EventError, match="unavailable"):
        f.execute()
    f.source_value["files"]["app/config.py"] = "VALUE = 1\n"
    with pytest.raises(EventError, match="unavailable"):
        f.execution(principal="e" * 64).execute(SPEC.plan_id, 3, **f.ref)
    f.now = EXPIRES
    with pytest.raises(EventError, match="expired"):
        f.execute()
    assert not f.client.submits


@pytest.mark.parametrize("change", [dict(input_digest="e" * 64), dict(runtime_source_sha="e" * 40),
    dict(request_id="other"), dict(proposal_digest="e" * 64), dict(lint_passed=False), dict(schema_valid=False),
    dict(lint_passed=1), dict(schema_valid=1)])
def test_mismatched_or_failed_result_is_not_persisted(fixture, change):
    f = fixture
    f.client.hook = lambda: f.client.envelope["result"].update(change)
    with pytest.raises(EventError, match="unconfirmed"):
        f.execute()
    assert f.result_rows() == 0


@pytest.mark.parametrize("change", [dict(source_sha="e" * 40), dict(proposal_digest="e" * 64),
    dict(request_id="other"), dict(plan_digest="e" * 64), dict(image_digest="not-published")])
def test_wrong_or_absent_candidate_cannot_transition(fixture, change):
    f = fixture
    f.execute()
    f.publish()
    f.published.update(change)
    with pytest.raises(EventError, match="publication_unconfirmed"):
        f.resume()
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "PROPOSE"


def test_result_write_failure_recovers_without_model_call(fixture, monkeypatch):
    f = fixture
    original = initial._put
    def fail(c, plan_id, key, request, receipt):
        if key.endswith(":result"):
            raise f.backend.storage_error("synthetic storage failure")
        return original(c, plan_id, key, request, receipt)
    monkeypatch.setattr(initial, "_put", fail)
    with pytest.raises(f.backend.storage_error):
        f.execute()
    monkeypatch.setattr(initial, "_put", original)
    f.port.read(SPEC.plan_id, 3, **f.ref)
    assert len(f.client.submits) == len(f.client.reads) == 1


def test_pending_read_failure_remains_claimed(fixture):
    f = fixture
    def pending():
        raise ExecutionPending("private upstream text")
    f.client.hook = pending
    with pytest.raises(ExecutionPending, match="^initial_execution_pending$"):
        f.execute()
    f.client.envelope = None
    for _ in range(2):
        with pytest.raises(EventError, match="unconfirmed"):
            f.execute()
    assert len(f.client.submits) == 1 and len(f.client.reads) == 2


def test_unrelated_rehearse_checkpoint_cannot_consume_saved_proposal(fixture):
    f = fixture
    f.execute()
    other = Evidence(SPEC.plan_id, SPEC.plan_digest, SPEC.target_environment_id, 3, "other-producer", ANALYSIS)
    with f.backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        initial._put(c, SPEC.plan_id, "workflow:3", {}, asdict(other))
        c.execute("UPDATE plans SET state='REHEARSE',version=4 WHERE plan_id=?", (SPEC.plan_id,))
    with pytest.raises(EventError, match="unavailable"):
        f.port.stored(SPEC.plan_id, 3, **f.ref)


def test_missing_analysis_checkpoint_never_admits(fixture):
    f = fixture
    with f.backend.admin() as c:
        c.execute("DELETE FROM plan_commands WHERE command_key='workflow:2'")
    with pytest.raises(EventError, match="unavailable"):
        f.admit()
    assert not f.client.submits


def test_wrong_runtime_cannot_replace_claim_or_promote(fixture):
    f = fixture
    f.execute()
    wrong = f.execution(runtime_source_sha="e" * 40)
    with pytest.raises(EventError, match="conflict"):
        wrong.execute(SPEC.plan_id, 3, **f.ref)
    with pytest.raises(EventError, match="conflict"):
        wrong.stored(SPEC.plan_id, 3, **f.ref)
    assert len(f.client.submits) == 1


def test_untrusted_receipt_and_disallowed_edit_cannot_be_stored(fixture):
    f = fixture
    f.client.hook = lambda: f.client.envelope.update(authenticated_fixture=False)
    with pytest.raises(EventError, match="unconfirmed"):
        f.execute()
    f.client.envelope["authenticated_fixture"] = True
    result = f.client.envelope["result"]
    result["proposal"]["edits"][0]["path"] = "app/other.py"
    result["proposal_digest"] = digest(result["proposal"])
    with pytest.raises(EventError, match="unconfirmed"):
        f.port.read(SPEC.plan_id, 3, **f.ref)
    assert f.result_rows() == 0


def test_result_validation_expiry_rolls_back_commit(fixture, monkeypatch):
    f = fixture
    original = f.client.validate
    def expire(*args):
        result = original(*args)
        f.now = EXPIRES
        return result
    monkeypatch.setattr(f.client, "validate", expire)
    with pytest.raises(EventError, match="expired"):
        f.execute()
    assert f.result_rows() == 0


def test_publication_read_expiry_prevents_state_transition(fixture):
    f = fixture
    f.execute()
    f.publish()
    def expired(*args):
        f.now = EXPIRES
        return f.published
    reader = f.port.evidence_reader(publication=expired)
    with pytest.raises(EventError, match="expired"):
        WorkflowRecovery(service(f.backend.store, None), read_evidence=reader).resume_once(PRINCIPAL, SPEC.plan_id)
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "PROPOSE"


def test_saved_proposal_retrieval_has_no_client_calls(fixture, monkeypatch):
    f = fixture
    result = f.execute()
    f.publish()
    f.resume()
    for method in ("recipient", "submit", "read", "validate"):
        monkeypatch.setattr(f.client, method, lambda *args: pytest.fail("stored reads must be local"))
    assert f.port.stored(SPEC.plan_id, 3, **f.ref) == result


def test_failed_claim_commit_never_dispatches(fixture):
    f = fixture
    with f.backend.reject_insert("plan_commands"), pytest.raises(f.backend.storage_error):
        f.execute()
    assert not f.client.submits and not f.client.reads
    f.execute()
    assert len(f.client.submits) == 1
