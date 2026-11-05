"""Real T10 admission and existing T15 consumer, with synthetic external effects."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
import test_initial_proposal as fixtures
from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.credential_proposal_source import BoundCredentialProposalSource
from rehearsal_runner.credential_verifier import CredentialVerificationRequest
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.runner import Namespace
from test_cutover import PRINCIPAL, SPEC

from bravo_platform_events.cutover import _get
from bravo_platform_events.initial_rehearsal import StoredInitialProposalSource
from bravo_platform_events.rehearsal import (
    Admission,
    RehearsalService,
    RehearsalTask,
    admitted_attempt,
)
from bravo_platform_events.store import EventError


class Flow:
    def __init__(self, backend, tmp_path):
        self.f = fixtures.Fixture(backend, tmp_path)
        self.effects = []
        self.allowed = True
        self.lose_ack = False
        self.host = T15RehearsalHost(self.f.catalog, SimpleNamespace(submit_once=self.submit),
                                    SimpleNamespace(read_authenticated=lambda _: None))

    def submit(self, admission, task):
        with self.f.backend.reopen()._transaction() as c:
            assert admitted_attempt(c, admission.plan_id, admission.rehearsal_id, admission.attempt) == admission
        assert task.run_id == admission.rehearsal_id
        self.effects.append(admission)
        if self.lose_ack:
            raise TimeoutError("synthetic lost reply")

    def access(self, *args):
        if not self.allowed:
            raise EventError("forbidden")

    def service(self, *, first_run=True):
        port = self.f.execution().rehearsal_run_id if first_run is True else first_run
        return RehearsalService(self.f.backend.reopen(), host=self.host, check_start=self.access,
                                first_run=port)

    def start(self, *, service=None, **changes):
        return (service or self.service()).start(PRINCIPAL, SPEC.plan_id,
            **(dict(task_id="credential", expected_version=4, attempt=1, command_key="first") | changes))

    def ready(self):
        self.result = self.f.execute()
        self.f.publish()
        assert self.f.resume() == "REHEARSE"

    def source(self, admission):
        return StoredInitialProposalSource(self.f.execution(), admission)


@pytest.fixture
def flow(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "SOURCE_PATH", "app/db_secret.py")
    return Flow(backend, tmp_path)


def test_actual_admission_preserves_proposal_and_existing_t15_consumes_after_restart(flow, monkeypatch):
    flow.ready()
    receipt = flow.start()
    admission, = flow.effects
    assert receipt["rehearsal_id"] == flow.result["proposal"]["run_id"]
    assert len(asdict(admission)) == 8 and admission.plan_version == 4
    source = flow.source(admission)
    request = CredentialVerificationRequest(admission.rehearsal_id, admission.source_sha,
        flow.result["proposal_digest"], Namespace("synthetic-rehearsal", "synthetic-uid"), admission.attempt)
    for name in ("recipient", "submit", "read", "validate"):
        monkeypatch.setattr(flow.f.client, name, lambda *args: pytest.fail("no second model/provider call"))
    consumer = BoundCredentialProposalSource(source)
    edits = consumer.resolve(request)
    assert edits[0].path == "app/db_secret.py" and edits[0].replacement == "VALUE = 2\n"
    assert consumer.resolve(request) == edits
    assert flow.start() == flow.start(command_key="new-key") == receipt
    assert len(flow.effects) == 1
    view = source.result()
    view["proposal"]["edits"][0]["replacement"] = "mutated"
    assert consumer.resolve(request) == edits


def test_lost_ack_and_concurrent_new_keys_keep_one_admission_and_one_model(flow):
    flow.ready()
    flow.lose_ack = True
    with pytest.raises(EventError, match="submission_unknown"):
        flow.start()
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda n: flow.start(command_key=f"reconnect-{n}"), range(6)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(flow.effects) == len(flow.f.client.submits) == 1
    assert not flow.f.client.reads


def test_no_admission_before_real_publication_or_without_initial_host_port(flow):
    flow.f.execute()
    with pytest.raises(EventError, match="not_allowed"):
        flow.start(expected_version=3)
    with pytest.raises(EventError, match="publication_unconfirmed"):
        flow.f.resume()
    flow.f.publish()
    flow.f.resume()
    with pytest.raises(EventError, match="reader_required"):
        flow.start(service=flow.service(first_run=None))
    assert not flow.effects


@pytest.mark.parametrize("field,value", [
    ("request_id", "wrong"), ("plan_digest", "0" * 64),
    ("source_sha", "a" * 40), ("proposal_digest", "a" * 64)])
def test_saved_publication_identity_is_rechecked_before_admission(flow, field, value):
    flow.ready()
    with flow.f.backend.store._transaction() as c:
        published = _get(c, SPEC.plan_id, "initial-proposal:3:publication")[1]
    published[field] = value  # These fields are not represented by the workflow Change.
    with flow.f.backend.admin() as c:
        sql = "UPDATE plan_commands SET receipt_json=? WHERE plan_id=? AND command_key=?"
        c.execute(sql.replace("?", "%s") if flow.f.backend.cluster else sql,
                  (json.dumps(published), SPEC.plan_id, "initial-proposal:3:publication"))
    with pytest.raises(EventError, match="publication_unconfirmed"):
        flow.start()
    assert not flow.effects


@pytest.mark.parametrize("fault", ["grant", "expiry", "source", "task", "human", "checkpoint"])
def test_current_inputs_and_authority_are_required_before_dispatch(flow, fault):
    flow.ready()
    if fault == "grant":
        flow.f.grant(False, 1)
    elif fault == "expiry":
        flow.f.now = fixtures.EXPIRES
    elif fault == "source":
        flow.f.source_value["files"]["app/db_secret.py"] = "changed"
    elif fault == "task":
        flow.host = SimpleNamespace(task=lambda *args: RehearsalTask("a" * 40, "b" * 64),
            submit=flow.host.submit, observe=flow.host.observe)
    elif fault == "human":
        flow.allowed = False
    else:
        with flow.f.backend.admin() as c:
            sql = "DELETE FROM plan_commands WHERE plan_id=? AND command_key='workflow:3'"
            c.execute(sql.replace("?", "%s") if flow.f.backend.cluster else sql, (SPEC.plan_id,))
    with pytest.raises(EventError):
        flow.start()
    assert not flow.effects


def test_uncommitted_admission_is_not_a_consumable_proposal(flow):
    flow.ready()
    with flow.f.backend.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        task = flow.f.catalog.for_plan(c, plan, "credential")
    invented = Admission(SPEC.plan_id, SPEC.tenant_id, 4, "credential", flow.result["proposal"]["run_id"],
                         1, task.source_sha, logical_task_digest(task))
    with pytest.raises(EventError):
        flow.source(invented).result()


def test_genuine_admission_from_other_run_cannot_consume_initial_proposal(flow):
    flow.ready()
    # Another trusted host path really commits an Admission, with valid source/task.
    # Admission identity checks alone must not authorize a different Proposal run.
    receipt = flow.start(service=flow.service(first_run=lambda *args: "other-admitted-run"))
    assert receipt["rehearsal_id"] != flow.result["proposal"]["run_id"]
    with pytest.raises(EventError, match="^initial_rehearsal_unavailable$"):
        flow.source(flow.effects[0]).result()


@pytest.mark.parametrize("change", [dict(plan_version=3), dict(tenant_id="foreign"),
    dict(task_id="foreign"), dict(rehearsal_id="foreign"), dict(attempt=2), dict(attempt=True),
    dict(source_sha="a" * 40), dict(task_digest="a" * 64)])
def test_consumer_refuses_admission_identity_changes(flow, change):
    flow.ready()
    flow.start()
    with pytest.raises(EventError):
        flow.source(replace(flow.effects[0], **change)).result()


@pytest.mark.parametrize("fault", ["grant", "expiry", "next-version", "next-state"])
def test_stored_consumer_rechecks_current_authority_and_immediate_rehearse(flow, fault):
    flow.ready()
    flow.start()
    if fault == "grant":
        flow.f.grant(False, 1)
    elif fault == "expiry":
        flow.f.now = fixtures.EXPIRES
    else:
        field, value = ("version", 5) if fault == "next-version" else ("state", "GATE")
        with flow.f.backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
            c.execute(f"UPDATE plans SET {field}=? WHERE plan_id=?", (value, SPEC.plan_id))
    with pytest.raises(EventError):
        flow.source(flow.effects[0]).result()


@pytest.mark.parametrize("fault", ["authority", "state", "invalid-run", "collision"])
def test_initial_run_callback_cannot_bypass_current_admission_checks(flow, fault):
    flow.ready()
    def first_run(c, plan, task_id, task):
        run = flow.f.port.rehearsal_run_id(c, plan, task_id, task)
        if fault == "authority":
            flow.allowed = False
        elif fault == "state":
            c.execute("UPDATE plans SET state='GATE' WHERE plan_id=?", (SPEC.plan_id,))
        elif fault == "invalid-run":
            return "invalid/run"
        else:
            from bravo_platform_events.cutover import _put
            _put(c, SPEC.plan_id, "rehearsal-run:" + run, {}, {"task_id": "other", "version": 4})
        return run
    with pytest.raises(EventError):
        flow.start(service=flow.service(first_run=first_run))
    assert not flow.effects


def test_failed_admission_commit_never_submits(flow):
    flow.ready()
    with flow.f.backend.reject_insert("plan_commands"), pytest.raises(flow.f.backend.storage_error):
        flow.start()
    assert not flow.effects
    assert flow.start()["rehearsal_id"] == flow.result["proposal"]["run_id"]
