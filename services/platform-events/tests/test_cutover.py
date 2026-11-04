"""Synthetic authority fixture only. No production gate/SHIFT/approval is minted."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from test_executor import EXECUTION, response_value

from bravo_platform_events.cutover import CurrentCutover, CutoverService
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.executor import ExecutorClient
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec
from bravo_platform_events.store import EventError

NOW = "2026-11-03T10:00:00+00:00"
EXPIRES = "2026-11-03T10:10:00+00:00"
PRINCIPAL = Principal("operator", "tenant-demo")
SPEC = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
BINDING = dict(plan_id=SPEC.plan_id, plan_digest=SPEC.plan_digest, diff_hash="sha256:" + "c" * 64,
               image_digest="sha256:" + "d" * 64, target_environment_id="env-onprem",
               gate_id="gate-1", context_digest="e" * 64, expires_at=EXPIRES)
BINDINGS = {"env-aws": "cutover/source", "env-onprem": "cutover/target"}
COMMAND = dict(plan_id=SPEC.plan_id, epoch=7,
               source=dict(environment_id="env-aws", connection_ref="cutover/source"),
               target=dict(environment_id="env-onprem", connection_ref="cutover/target"),
               write_pause_budget_ms=30000, deadline=EXPIRES)


class Fixture:
    def __init__(self, backend):
        self.backend = backend
        self.now = NOW
        self.context = CurrentCutover(dict(BINDING), json.loads(json.dumps(COMMAND)), "authority-rev1")
        self.allowed = True
        self.requests = []
        self.value = response_value()
        self.fail_post = False
        self.empty_get = False
        self.checked_actors = []
        backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
        # Test-only prerequisite seed. No runtime route or convenience method can do this.
        with backend.admin() as c:
            c.execute("UPDATE plans SET state='SHIFT' WHERE plan_id='plan-demo'")
        self.client = ExecutorClient("https://executor.example.invalid", token="fixture-read", command_token="fixture-command",
                                     transport=httpx.MockTransport(self.handle))

    def authority(self, c, principal, plan, *, now, approved_by, approval_id):
        self.checked_actors.append((principal.subject_id, approved_by))
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone()["state"] == "SHIFT"
        if not self.allowed:
            raise RuntimeError("PRIVATE_PERMISSION_ERROR")
        return self.context

    def handle(self, request):
        self.requests.append(request)
        if request.method == "POST":
            # Durable command and CUTOVER event exist before any remote side effect.
            assert self.backend.reopen().snapshot(SPEC.plan_id)["state"] == "CUTOVER"
            if self.fail_post:
                raise httpx.ReadTimeout("PRIVATE_RESPONSE_LOSS")
            return httpx.Response(202, json=self.value)
        if self.empty_get:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[self.value] if request.url.path == "/v1/cutovers" else self.value)

    def service(self):
        return CutoverService(self.backend.reopen(), self.client, bindings=BINDINGS,
                              authority=self.authority, clock=lambda: self.now)

    def approve(self, **changes):
        args = dict(binding=BINDING, expected_version=1, command_key="approve-1")
        return self.service().approve(PRINCIPAL, SPEC.plan_id, **(args | changes))

    def submit(self, approval, **changes):
        args = dict(approval_id=approval["approval_id"], command_key="submit-1")
        return self.service().submit(PRINCIPAL, SPEC.plan_id, **(args | changes))


@pytest.fixture
def fixture(backend):
    f = Fixture(backend)
    yield f
    f.client.close()


def test_approval_then_single_post_and_durable_projection(fixture):
    f = fixture
    approval = f.approve()
    assert f.approve() == approval
    receipt = f.submit(approval)
    assert receipt["snapshot"]["execution_id"] == EXECUTION
    assert len(f.requests) == 1
    assert json.loads(f.requests[0].content) == COMMAND
    assert f.requests[0].headers["Idempotency-Key"] == receipt["command_id"]
    assert f.checked_actors[-1] == ("operator", "operator")
    assert "PRIVATE" not in json.dumps(receipt)
    # Reopen both DB and service; retry reads saved identity, never dispatches again.
    assert f.submit(approval) == receipt
    assert [r.method for r in f.requests] == ["POST", "GET"]
    events = f.backend.store.read_after(SPEC.plan_id, None)
    assert [e["event_type"] for e in events] == ["state.changed", "approval.decided", "state.changed", "execution.updated"]
    with f.backend.admin() as c:
        count = c.execute("SELECT COUNT(*) AS n FROM plan_commands").fetchone()[0]
    f.submit(approval)
    with f.backend.admin() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM plan_commands").fetchone()[0] == count


@pytest.mark.parametrize("field", ["plan_digest", "diff_hash", "image_digest", "gate_id", "context_digest", "target_environment_id", "expires_at"])
def test_changed_binding_is_rejected_without_effect(fixture, field):
    f = fixture
    with pytest.raises(EventError, match="binding_mismatch"):
        f.approve(binding=BINDING | {field: "different"})
    assert not f.requests
    assert len(f.backend.store.read_after(SPEC.plan_id, None)) == 1


def test_authority_default_denies_and_masks_failure(fixture):
    f = fixture
    service = CutoverService(f.backend.store, f.client, bindings=BINDINGS, clock=lambda: NOW)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(PRINCIPAL, SPEC.plan_id, binding=BINDING, expected_version=1, command_key="key")
    f.allowed = False
    with pytest.raises(EventError, match="^authority_unavailable$"):
        f.approve()
    assert not f.requests


@pytest.mark.parametrize("fault", ["expired", "revoked", "revision", "binding", "version", "state", "pair"])
def test_consume_rechecks_current_authority_and_binding(fixture, fault):
    f = fixture
    approval = f.approve()
    if fault == "expired":
        f.now = EXPIRES
    elif fault == "revoked":
        f.allowed = False
    elif fault == "revision":
        f.context = replace(f.context, revision="rev2")
    elif fault == "binding":
        f.context = replace(f.context, binding=BINDING | {"gate_id": "gate-new"})
    elif fault == "pair":
        f.context.command["source"]["connection_ref"] = "other/source"
    else:
        with f.backend.admin() as c:
            c.execute("UPDATE plans SET version=2" if fault == "version" else "UPDATE plans SET state='SAFE_HOLD'")
    with pytest.raises(EventError):
        f.submit(approval)
    assert not f.requests


def test_foreign_tenant_never_calls_authority_or_executor(fixture):
    f = fixture
    with pytest.raises(EventError, match="forbidden"):
        f.service().approve(Principal("other", "foreign"), SPEC.plan_id, binding=BINDING,
                            expected_version=1, command_key="key")
    assert not f.checked_actors and not f.requests


def test_response_loss_only_get_after_restart_and_hold_is_not_cleared(fixture):
    f = fixture
    approval = f.approve()
    f.fail_post = True
    result = f.submit(approval)
    assert result["outcome"] == "UNKNOWN"
    assert f.backend.reopen().snapshot(SPEC.plan_id)["state"] == "SAFE_HOLD"
    f.value.update(status="DONE", committed=True)
    result = f.submit(approval)
    assert result["snapshot"]["remote_status"] == "DONE"
    assert f.backend.reopen().snapshot(SPEC.plan_id)["state"] == "SAFE_HOLD"
    assert [r.method for r in f.requests] == ["POST", "GET"]


def test_crash_after_durable_claim_before_post_never_dispatches_on_retry(fixture):
    f = fixture
    approval = f.approve()
    record, created = f.service()._prepare(PRINCIPAL, SPEC.plan_id, approval["approval_id"], "submit-1")
    assert created
    f.empty_get = True
    result = f.submit(approval)
    assert result["command_id"] == record["command_id"]
    assert result["outcome"] == "UNKNOWN"
    assert [r.method for r in f.requests] == ["GET"]


def test_concurrent_submit_sends_one_post(fixture):
    f = fixture
    approval = f.approve()
    barrier = Barrier(6)
    def submit(_):
        barrier.wait(timeout=10)
        return f.submit(approval)
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(submit, range(6)))
    assert len({r["command_id"] for r in receipts}) == 1
    assert sum(r.method == "POST" for r in f.requests) == 1
    assert len([e for e in f.backend.store.read_after(SPEC.plan_id, None) if e["event_type"] == "execution.updated"]) == 1


def test_rewritten_history_is_unknown_and_no_step_is_overwritten(fixture):
    f = fixture
    approval = f.approve()
    original = f.submit(approval)
    f.value["steps"][0]["elapsed_ms"] = 999
    result = f.submit(approval)
    assert result["outcome"] == "UNKNOWN"
    assert result["snapshot"] == original["snapshot"]
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "SAFE_HOLD"


def test_new_step_is_projected_once_with_first_observation_time(fixture):
    f = fixture
    approval = f.approve()
    f.submit(approval)
    f.now = "2026-11-03T10:00:02+00:00"
    f.value["steps"].append(dict(step="fence", status="started"))
    f.value["updated_at"] = f.now
    f.submit(approval)
    f.now = "2026-11-03T10:00:03+00:00"
    f.submit(approval)
    events = [e for e in f.backend.store.read_after(SPEC.plan_id, None) if e["event_type"] == "execution.updated"]
    assert len(events) == 2 and events[-1]["occurred_at"] == "2026-11-03T10:00:02+00:00"


@pytest.mark.parametrize("table", ["events", "plan_commands"])
def test_store_failure_rolls_back_before_dispatch(fixture, table):
    f = fixture
    approval = f.approve()
    with f.backend.reject_insert(table), pytest.raises(f.backend.storage_error):
        f.submit(approval)
    assert not f.requests
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "SHIFT"
    assert f.submit(approval)["outcome"] == "OBSERVED"


def test_api_denies_unverified_identity_and_change_approval(fixture):
    f = fixture
    body = dict(approval_kind="cutover", binding=BINDING, expected_version=1)
    url = f"/v1/plans/{SPEC.plan_id}/approvals"
    with TestClient(create_cutover_app(f.service())) as client:
        assert client.post(url, json=body, headers={"Idempotency-Key": "a", "X-Tenant-ID": SPEC.tenant_id}).status_code == 403
    with TestClient(create_cutover_app(f.service(), resolve_principal=lambda _: PRINCIPAL)) as client:
        response = client.post(url, json=body | {"approval_kind": "change"}, headers={"Idempotency-Key": "a"})
        assert response.status_code == 422
        response = client.post(url, json=body, headers={"Idempotency-Key": "a"})
        assert response.status_code == 200
        assert "context" not in response.json()
        approval_id = response.json()["approval_id"]
        result = client.post(f"/v1/plans/{SPEC.plan_id}/cutover", json={"approval_id": approval_id}, headers={"Idempotency-Key": "s"})
        assert result.status_code == 202
        replay = client.get(f"/v1/plans/{SPEC.plan_id}/events?follow=false", headers={"Last-Event-ID": "plan-demo:3"})
        assert "execution.updated" in replay.text


def test_different_input_reusing_key_and_second_consumption_are_rejected(fixture):
    f = fixture
    approval = f.approve()
    with pytest.raises(EventError, match="command_key_conflict"):
        f.approve(expected_version=2)
    f.submit(approval)
    with pytest.raises(EventError, match="command_key_conflict"):
        f.submit({"approval_id": "different"})
    with pytest.raises(EventError, match="approval_unavailable"):
        f.submit(approval, command_key="another")
    assert sum(r.method == "POST" for r in f.requests) == 1


def test_projection_failure_does_not_repeat_accepted_post(fixture):
    f = fixture
    approval = f.approve()
    original = f.client.submit

    def accepted_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated_process_loss")

    f.client.submit = accepted_then_crash
    with pytest.raises(RuntimeError, match="simulated_process_loss"):
        f.submit(approval)
    f.client.submit = original
    assert f.submit(approval)["snapshot"]["execution_id"] == EXECUTION
    assert [r.method for r in f.requests] == ["POST", "GET"]


def test_remote_safe_hold_does_not_complete_plan(fixture):
    f = fixture
    f.value.update(status="SAFE_HOLD", committed=True)
    assert f.submit(f.approve())["snapshot"]["remote_status"] == "SAFE_HOLD"
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "SAFE_HOLD"


def test_binding_expiring_during_authority_lookup_is_rejected(fixture):
    f = fixture
    authority = f.authority
    def advance_clock(*args, **kwargs):
        result = authority(*args, **kwargs)
        f.now = EXPIRES
        return result
    f.authority = advance_clock
    with pytest.raises(EventError, match="approval_expired"):
        f.approve()
    assert not f.requests
