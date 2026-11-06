"""Actual T10 HTTP adapter with synthetic host identity and prerequisite authority."""

from datetime import UTC, datetime, timedelta

import pytest
from bravo_platform_events.cutover import CurrentCutover, CutoverService
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.executor import ExecutionRead
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore
from fastapi.testclient import TestClient

from bravo_console import ConsoleApp

ORIGIN = "https://console.example.invalid"
CHANGE = {
    "Origin": ORIGIN,
    "X-Bravo-Console": "1",
    "Content-Type": "application/json",
    "Idempotency-Key": "command-one",
}


@pytest.fixture
def fixture(tmp_path):
    return setup(tmp_path / "state.db")


def setup(path, origin=ORIGIN, workload_id="chat", plan_id="plan-demo", profile=None):
    store = PlanStore(path)
    now = datetime.now(UTC)
    spec = PlanSpec(
        plan_id, "tenant-demo", workload_id, "env-source", "env-target", "a" * 64, "b" * 64,
        *(profile or (None, None)),
    )
    store.register_plan(spec, run_id="setup", occurred_at=now.isoformat())
    state = {"allowed": True, "submitted": 0, "reads": 0}
    binding = dict(
        plan_id=spec.plan_id,
        plan_digest=spec.plan_digest,
        diff_hash="sha256:" + "c" * 64,
        image_digest="sha256:" + "d" * 64,
        target_environment_id="env-target",
        gate_id="gate-one",
        context_digest="e" * 64,
        expires_at=(now + timedelta(minutes=10)).isoformat(),
    )
    # Only this test setup seeds prerequisites. Production ConsoleApp has no seeding API.
    with store._transaction(write=True, plan_id=spec.plan_id) as c:
        c.execute("UPDATE plans SET state='SHIFT', version=2 WHERE plan_id=?", (spec.plan_id,))
        store._append_in_transaction(
            c,
            plan_id=spec.plan_id,
            run_id="setup",
            event_type="state.changed",
            data={"from": "COLLECT", "to": "SHIFT"},
            occurred_at=now.isoformat(),
            producer_key="shift",
            state_owner=True,
        )
    store.append(
        plan_id=spec.plan_id,
        run_id="setup",
        event_type="gate.completed",
        data=dict(
            gate_id="gate-one",
            context_digest="e" * 64,
            decision="PASS",
            evidence_ref="evidence-one",
        ),
        occurred_at=now.isoformat(),
        producer_key="gate",
    )
    store.append(
        plan_id=spec.plan_id,
        run_id="setup",
        event_type="approval.requested",
        data=dict(approval_id="request-one", approval_kind="cutover", binding=binding),
        occurred_at=now.isoformat(),
        producer_key="request",
    )
    command = dict(
        plan_id=spec.plan_id,
        epoch=1,
        source=dict(environment_id="env-source", connection_ref="registered/source"),
        target=dict(environment_id="env-target", connection_ref="registered/target"),
        write_pause_budget_ms=30000,
        deadline=binding["expires_at"],
    )

    class Executor:
        def submit(self, *_args, **_kwargs):
            state["submitted"] += 1
            return ExecutionRead("UNKNOWN", "test_response_loss")

        def read(self, *_args, **_kwargs):
            state["reads"] += 1
            return ExecutionRead("UNKNOWN", "test_response_loss")

    service = CutoverService(
        store,
        Executor(),
        bindings={"env-source": "registered/source", "env-target": "registered/target"},
        authority=lambda *_a, **_k: CurrentCutover(binding, command, "fixture-revision"),
    )

    def principal(request):
        if state["allowed"] and request.cookies.get("fixture-session") == "test-only":
            return Principal("fixture-operator", "tenant-demo")
        return None

    api = create_cutover_app(service, resolve_principal=principal)
    return ConsoleApp(api, public_origin=origin), store, state, binding


def client(fixture):
    app, *_ = fixture
    return TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"})


def test_default_t10_authentication_is_not_replaced_by_console(tmp_path):
    api = create_plan_app(PlanStore(tmp_path / "empty.db"))
    with TestClient(ConsoleApp(api, public_origin=ORIGIN), base_url=ORIGIN) as c:
        assert c.get("/console/").status_code == 200
        response = c.get(
            "/v1/plans/plan-demo", headers={"X-Bravo-Console": "1", "X-Tenant-ID": "tenant-demo"}
        )
        assert response.status_code == 403


def test_real_t10_approval_dispatch_response_loss_and_readback(fixture):
    _, store, state, binding = fixture
    with client(fixture) as c:
        snap = c.get("/v1/plans/plan-demo").json()
        body = dict(approval_kind="cutover", binding=binding, expected_version=snap["version"])
        approval = c.post("/v1/plans/plan-demo/approvals", json=body, headers=CHANGE)
        assert approval.status_code == 200
        assert state["submitted"] == 0
        again = c.post("/v1/plans/plan-demo/approvals", json=body, headers=CHANGE)
        assert approval.json() == again.json()
        execute = {"approval_id": approval.json()["approval_id"]}
        headers = CHANGE | {"Idempotency-Key": "execute-one"}
        response = c.post("/v1/plans/plan-demo/cutover", json=execute, headers=headers)
        assert response.status_code == 202 and response.json()["outcome"] == "UNKNOWN"
        assert state["submitted"] == 1 and store.snapshot("plan-demo")["state"] == "SAFE_HOLD"
        c.post("/v1/plans/plan-demo/cutover", json=execute, headers=headers)
        assert state["submitted"] == 1 and state["reads"] == 1
        command = response.json()["command_id"]
        read = c.post(f"/v1/plans/plan-demo/cutover/{command}/reconcile", json={}, headers=CHANGE)
        assert read.status_code == 200 and state["reads"] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "no-origin",
        "foreign-origin",
        "foreign-fetch-site",
        "missing-header",
        "plain-form",
        "wrong-host",
        "duplicate-origin",
        "too-large",
    ],
)
def test_browser_boundary_prevents_dispatch(fixture, fault):
    _, _, state, binding = fixture
    headers = dict(CHANGE)
    if fault == "no-origin":
        headers.pop("Origin")
    elif fault == "foreign-origin":
        headers["Origin"] = "https://attacker.invalid"
    elif fault == "foreign-fetch-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif fault == "missing-header":
        headers.pop("X-Bravo-Console")
    elif fault == "plain-form":
        headers["Content-Type"] = "text/plain"
    elif fault == "wrong-host":
        headers["Host"] = "attacker.invalid"
    elif fault == "duplicate-origin":
        headers = list(headers.items()) + [("Origin", ORIGIN)]
    with client(fixture) as c:
        response = c.post(
            "/v1/plans/plan-demo/approvals",
            headers=headers,
            content="x" * 65537 if fault == "too-large" else "{}",
        )
        assert response.status_code in (400, 403, 413, 421)
        assert state["submitted"] == 0
        events = c.get("/v1/plans/plan-demo/events?follow=false").text
        assert '"event_type":"approval.decided"' not in events


def test_revoked_host_access_blocks_snapshot_sse_and_mutation(fixture):
    _, _, state, _ = fixture
    with client(fixture) as c:
        assert c.get("/v1/plans/plan-demo").status_code == 200
        state["allowed"] = False
        for path in ("/v1/plans/plan-demo", "/v1/plans/plan-demo/events?follow=false"):
            response = c.get(path)
            assert response.status_code == 403 and "chat" not in response.text
        assert (
            c.post(
                "/v1/plans/plan-demo/approvals",
                json=dict(approval_kind="cutover", binding={}, expected_version=2),
                headers=CHANGE,
            ).status_code
            == 403
        )


def test_sse_cursor_replay_and_headers_preserved(fixture):
    with client(fixture) as c:
        response = c.get(
            "/v1/plans/plan-demo/events?follow=false", headers={"Last-Event-ID": "plan-demo:3"}
        )
        assert response.status_code == 200 and "id: plan-demo:4" in response.text
        assert "id: plan-demo:3" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_static_assets_are_allowlisted_and_no_remote_scripts(fixture):
    with client(fixture) as c:
        for path in (
            "/console/",
            "/console/model.js",
            "/console/app.js",
            "/console/transport.js",
            "/console/style.css",
        ):
            response = c.get(path)
            assert (
                response.status_code == 200
                and response.headers["x-content-type-options"] == "nosniff"
            )
        assert c.get("/console/../../../pyproject.toml").status_code == 404
        assert c.get("/console/missing.js").status_code == 404
        assert c.get("/console/", headers={"Host": "evil.invalid"}).status_code == 421


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example.invalid",
        "https://host.invalid/path",
        "https://a:b@host.invalid",
        "https://host.invalid?url=x",
        "https://host.invalid/#x",
    ],
)
def test_public_origin_must_be_fixed_and_secure(origin):
    with pytest.raises(ValueError):
        ConsoleApp(None, public_origin=origin)
