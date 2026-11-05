"""Real journals, synthetic Actions effects: one dispatch and bounded recovery."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest
from bravo_agent.contracts import Proposal, digest
from bravo_agent.product_execution_contracts import (
    ExecutionPending,
    ExecutionUnknown,
    receipt_binding,
)
from test_cutover import EXPIRES
from test_product_inputs import PRINCIPAL, Fixture

from bravo_platform_events import product_execution as execution
from bravo_platform_events.store import EventError

RUNTIME = "c" * 40


def result_for(intent, value):
    proposal = Proposal(run_id=value.bundle.run_id, source_sha=value.bundle.source_sha,
        rationale="Fixture proposal", risk="low", edits=[dict(path="policy.json",
        before_sha256=value.manifest.files["policy.json"], replacement='{"Statement":[{"Effect":"Deny","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::example-bucket-123456789012/*"}]}')])
    bundle = value.bundle.model_dump(mode="json")
    result = dict(state="PROPOSAL_READY", input_kind="product", attempt=1, run_id=value.bundle.run_id,
        source_sha=value.bundle.source_sha, input=bundle, input_digest=digest(value.bundle),
        task=bundle, task_digest=digest(value.bundle), snapshot_digest=digest(value.manifest),
        proposal=proposal.model_dump(mode="json"), proposal_digest=digest(proposal),
        product_input_digest=intent.input_digest, admission_digest=intent.admission_digest,
        runtime_source_sha=RUNTIME, tool_events_status="UNAVAILABLE")
    return receipt_binding(intent, "100") | dict(status=200, cleanup_confirmed=True, result=result)


class Actions:
    def __init__(self, value):
        self.value, self.submits, self.reads = value, [], []
        self.envelope, self.lose_response, self.hook = None, False, lambda: None
        self.keys = []

    def recipient(self, request_id):
        self.keys.append(request_id)
        return "f" * 64

    def submit(self, intent):
        self.submits.append(intent)
        self.envelope = result_for(intent, self.value)
        self.hook()
        if self.lose_response:
            raise RuntimeError("private provider diagnostic")
        return deepcopy(self.envelope)

    def read(self, intent):
        self.reads.append(intent)
        return deepcopy(self.envelope)


@pytest.fixture
def setup(backend):
    f = Fixture(backend)
    value = f.value()
    ref = f.register(value)
    client = Actions(value)
    port = execution.ProductExecution(f.records(), client, principal=PRINCIPAL, runtime_source_sha=RUNTIME)
    return f, ref, client, port


def test_completed_receipt_survives_restart_without_new_dispatch(setup):
    f, ref, client, port = setup
    result = port.execute(f.admission, **ref)
    restarted = execution.ProductExecution(f.records(), client, principal=PRINCIPAL, runtime_source_sha=RUNTIME)
    assert restarted.read(f.admission, **ref) == result
    assert restarted.execute(f.admission, **ref) == result
    assert len(client.submits) == 1 and not client.reads
    assert len(client.keys) == 1
    assert client.submits[0].mode == "propose"
    assert "token" not in str(client.submits[0].model_dump())
    f.allowed = False
    with pytest.raises(EventError, match="product_input_unavailable"):
        restarted.read(f.admission, **ref)
    assert len(client.submits) == 1


def test_lost_dispatch_response_requeries_only_the_original_intent(setup):
    f, ref, client, port = setup
    client.lose_response = True
    with pytest.raises(EventError, match="^product_execution_unconfirmed$"):
        port.execute(f.admission, **ref)
    result = port.execute(f.admission, **ref)
    assert result["state"] == "PROPOSAL_READY"
    assert client.submits == client.reads
    assert len(client.submits) == 1
    assert len(client.keys) == 1


def test_concurrent_call_reads_the_effect_while_first_response_is_pending(setup):
    f, ref, client, port = setup
    started, release = Event(), Event()
    def response_pending():
        started.set()
        assert release.wait(5)
    client.hook = response_pending
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(port.execute, f.admission, **ref)
        assert started.wait(5)
        try:
            second = execution.ProductExecution(f.records(), client, principal=PRINCIPAL,
                                               runtime_source_sha=RUNTIME)
            result = second.execute(f.admission, **ref)
        finally:
            release.set()
        assert first.result() == result
    assert len(client.submits) == len(client.reads) == 1


def test_unknown_without_a_remote_result_never_redispatches(setup):
    f, ref, client, port = setup
    client.lose_response = True
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.execute(f.admission, **ref)
    client.envelope = None
    for op in (port.execute, port.read):
        with pytest.raises(EventError, match="product_execution_unconfirmed"):
            op(f.admission, **ref)
    assert len(client.submits) == 1 and len(client.reads) == 2


def test_crash_after_claim_before_external_effect_does_not_submit_again(setup, monkeypatch):
    f, ref, client, port = setup
    class ProcessStopped(BaseException):
        pass
    def stopped(intent):
        client.submits.append(intent)
        raise ProcessStopped()
    monkeypatch.setattr(client, "submit", stopped)
    with pytest.raises(ProcessStopped):
        port.execute(f.admission, **ref)
    restarted = execution.ProductExecution(f.records(), client, principal=PRINCIPAL, runtime_source_sha=RUNTIME)
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        restarted.execute(f.admission, **ref)
    assert client.envelope is None and len(client.submits) == len(client.reads) == 1


def test_permission_revoked_during_external_call_blocks_result_commit(setup):
    f, ref, client, port = setup
    client.hook = lambda: setattr(f, "allowed", False)
    with pytest.raises(EventError, match="product_input_unavailable"):
        port.execute(f.admission, **ref)
    assert len(client.submits) == 1
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("product-execution:%:result",)).fetchone()["n"] == 0


def test_read_cannot_create_claim_and_different_runtime_cannot_replace_one(setup):
    f, ref, client, port = setup
    with pytest.raises(EventError, match="product_execution_not_started"):
        port.read(f.admission, **ref)
    assert not client.submits and not client.reads
    port.execute(f.admission, **ref)
    other = execution.ProductExecution(f.records(), client, principal=PRINCIPAL, runtime_source_sha="e" * 40)
    with pytest.raises(EventError, match="product_execution_conflict"):
        other.execute(f.admission, **ref)
    assert len(client.submits) == 1


@pytest.mark.parametrize("change", [dict(attempt=2), dict(tenant_id="foreign"), dict(plan_version=9)])
def test_different_admission_is_rejected_before_any_action(setup, change):
    f, ref, client, port = setup
    with pytest.raises(EventError):
        port.execute(replace(f.admission, **change), **ref)
    assert not client.submits and not client.reads


@pytest.mark.parametrize("change", [dict(cleanup_confirmed=False), dict(github_run_attempt=2),
    dict(admission_digest="e" * 64), dict(runtime_source_sha="e" * 40),
    dict(recipient_key_digest="e" * 64), dict(status=503)])
def test_unbound_or_unclean_receipt_never_becomes_a_completed_result(setup, change):
    f, ref, client, port = setup
    client.hook = lambda: client.envelope.update(change)
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.execute(f.admission, **ref)
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.read(f.admission, **ref)
    assert len(client.submits) == 1 and len(client.reads) == 1


def test_source_snapshot_binding_and_expiry_after_remote_call_are_current(setup):
    f, ref, client, port = setup
    client.hook = lambda: client.envelope["result"].update(snapshot_digest="e" * 64)
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.execute(f.admission, **ref)
    client.envelope = result_for(client.submits[0], client.value)
    f.now = EXPIRES
    with pytest.raises(EventError, match="product_input_unavailable"):
        port.read(f.admission, **ref)
    assert not client.reads


def test_result_commit_failure_recovers_from_same_remote_effect(setup, monkeypatch):
    f, ref, client, port = setup
    original = execution._put
    def reject_result(c, plan_id, key, request, receipt):
        if key.endswith(":result"):
            raise f.backend.storage_error("fixture storage failure")
        return original(c, plan_id, key, request, receipt)
    monkeypatch.setattr(execution, "_put", reject_result)
    with pytest.raises(f.backend.storage_error):
        port.execute(f.admission, **ref)
    monkeypatch.setattr(execution, "_put", original)
    assert port.read(f.admission, **ref)["state"] == "PROPOSAL_READY"
    assert len(client.submits) == len(client.reads) == 1


class Time:
    now = 0
    def clock(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def test_call_waits_by_reading_only_and_returns_the_same_proposal(setup, monkeypatch):
    f, ref, client, port = setup
    original = client.submit
    def pending(intent):
        original(intent)
        raise ExecutionPending("private upstream text")
    monkeypatch.setattr(client, "submit", pending)
    clock = Time()
    call = execution.ActionsProductCall(port, wait_seconds=3, poll_seconds=1,
                                        clock=clock.clock, sleep=clock.sleep)
    status, result = call(ref)
    assert status == 200 and result["run_id"] == f.admission.rehearsal_id
    assert len(client.submits) == len(client.reads) == 1 and clock.now == 1
    with pytest.raises(EventError, match="product_execution_mismatch"):
        call(ref | {"web_identity_token": "must not cross this boundary"})


@pytest.mark.parametrize("unknown", [False, True])
def test_deadline_or_unknown_never_reissues_the_workflow(setup, monkeypatch, unknown):
    _, ref, client, port = setup
    original = client.submit
    def pending(intent):
        original(intent)
        if unknown:
            raise ExecutionUnknown("private upstream text")
        raise ExecutionPending("private upstream text")
    def read_pending(intent):
        client.reads.append(intent)
        raise ExecutionPending("private upstream text")
    monkeypatch.setattr(client, "submit", pending)
    monkeypatch.setattr(client, "read", read_pending)
    clock = Time()
    call = execution.ActionsProductCall(port, wait_seconds=3, poll_seconds=1,
                                        clock=clock.clock, sleep=clock.sleep)
    with pytest.raises(EventError, match="^product_execution_unconfirmed$"):
        call(ref)
    assert len(client.submits) == 1
    assert len(client.reads) == (0 if unknown else 3)


def test_missing_recipient_rejects_before_claim_or_dispatch(setup, monkeypatch):
    f, ref, client, port = setup
    monkeypatch.setattr(client, "recipient", lambda _: "invalid public key")
    with pytest.raises(EventError, match="^product_recipient_unavailable$"):
        port.execute(f.admission, **ref)
    assert not client.submits and not client.reads
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("product-execution:%",)).fetchone()["n"] == 0


def test_existing_intent_never_prepares_a_replacement_key(setup, monkeypatch):
    f, ref, client, port = setup
    client.lose_response = True
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.execute(f.admission, **ref)
    def no_new_key(_):
        pytest.fail("Existing execution must keep its original recipient")
    monkeypatch.setattr(client, "recipient", no_new_key)
    assert port.read(f.admission, **ref)["state"] == "PROPOSAL_READY"
    assert len(client.keys) == len(client.submits) == len(client.reads) == 1


def test_preparation_is_read_only_and_matches_the_later_committed_intent(setup, monkeypatch):
    f, ref, client, port = setup
    def commands():
        with f.backend.store._transaction() as c:
            return [dict(row) for row in c.execute("SELECT * FROM plan_commands ORDER BY command_key").fetchall()]
    before = commands()
    with monkeypatch.context() as patch:
        for name in ("recipient", "submit", "read"):
            patch.setattr(client, name, lambda _: pytest.fail("Preparation must not call the client"))
        fields = port.preparation_fields(f.admission, **ref)
        assert fields == port.preparation_fields(f.admission, **ref)
        assert set(fields) == {"request_id", "input_ref", "input_digest", "admission_digest", "runtime_source_sha", "mode"}
        assert commands() == before
    assert port.execute(f.admission, **ref)["state"] == "PROPOSAL_READY"
    assert client.submits[0].model_dump(exclude={"recipient_public_key"}) == fields


def test_preparation_does_not_reserve_or_extend_authority(setup):
    f, ref, client, port = setup
    port.preparation_fields(f.admission, **ref)
    f.allowed = False
    with pytest.raises(EventError, match="product_input_unavailable"):
        port.preparation_fields(f.admission, **ref)
    with pytest.raises(EventError, match="product_input_unavailable"):
        port.execute(f.admission, **ref)
    assert not client.keys and not client.submits and not client.reads


def test_claimed_attempt_cannot_prepare_a_new_mailbox_or_recipient(setup):
    f, ref, client, port = setup
    client.lose_response = True
    with pytest.raises(EventError, match="product_execution_unconfirmed"):
        port.execute(f.admission, **ref)
    with pytest.raises(EventError, match="^product_execution_already_claimed$"):
        port.preparation_fields(f.admission, **ref)
    assert len(client.keys) == len(client.submits) == 1 and not client.reads
    assert port.read(f.admission, **ref)["state"] == "PROPOSAL_READY"
    assert len(client.submits) == len(client.reads) == 1
