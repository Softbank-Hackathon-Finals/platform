"""Real T10 change approval route; host identity and verification are fixtures."""

from bravo_platform_events.change import ChangeApprovalService, CurrentChange
from bravo_platform_events.cutover import CutoverService
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.plan_api import Principal
from fastapi.testclient import TestClient
from test_app import CHANGE, ORIGIN, setup

from bravo_console import ConsoleApp


def setup_change(path, *, origin=ORIGIN, plan_id="plan-change"):
    _, store, state, binding = setup(path, origin=origin, plan_id=plan_id)
    binding = {k: v for k, v in binding.items() if k not in ("gate_id", "context_digest")}
    now = store.snapshot(plan_id)["updated_at"]
    with store._transaction(write=True, plan_id=plan_id) as c:
        c.execute("UPDATE plans SET state='REHEARSE', version=3 WHERE plan_id=?", (plan_id,))
        store._append_in_transaction(c, plan_id=plan_id, run_id="change-run",
            event_type="state.changed", data={"from": "SHIFT", "to": "REHEARSE"},
            occurred_at=now, producer_key="rehearse", state_owner=True)
    store.append(plan_id=plan_id, run_id="change-run", event_type="approval.requested",
        data={"approval_id": "change-request", "approval_kind": "change", "binding": binding},
        occurred_at=now, producer_key="change-request")

    def authority(*_a, **_k):
        return CurrentChange(binding, "change-run", "fixture-verification", "fixture-revision")

    def principal(request):
        if state["allowed"] and request.cookies.get("fixture-session") == "test-only":
            return Principal("fixture-operator", "tenant-demo")
        return None

    # A sentinel: approving must not even call a deployment or executor port.
    class Executor:
        def submit(self, *_a, **_k):
            state["submitted"] += 1
            raise AssertionError("change approval cannot execute")

    api = create_cutover_app(CutoverService(store, Executor(), bindings={
        "env-source": "registered/source", "env-target": "registered/target",
    }), resolve_principal=principal,
        change_service=ChangeApprovalService(store, authority=authority))
    return ConsoleApp(api, public_origin=origin), store, state, binding


def test_change_approval_real_api_idempotency_run_binding_and_no_dispatch(tmp_path):
    app, store, state, binding = setup_change(tmp_path / "state.db")
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        body = dict(approval_kind="change", run_id="change-run", binding=binding, expected_version=3)
        path = "/v1/plans/plan-change/approvals"
        first = c.post(path, json=body, headers=CHANGE)
        assert first.status_code == 200
        assert first.json()["run_id"] == "change-run"
        assert first.json()["approval_kind"] == "change"
        assert c.post(path, json=body, headers=CHANGE).json() == first.json()
        other = c.post(path, json={**body, "run_id": "other-run"},
            headers=CHANGE | {"Idempotency-Key": "other-run"})
        assert other.status_code == 409
        assert state["submitted"] == 0 and store.snapshot("plan-change")["state"] == "REHEARSE"
        events = c.get("/v1/plans/plan-change/events?follow=false").text
        assert '"approval_kind":"change"' in events and '"event_type":"approval.decided"' in events
        state["allowed"] = False
        assert c.post(path, json=body, headers=CHANGE).status_code == 403
