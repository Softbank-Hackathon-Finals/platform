"""Real T10 session/plan checks with an explicitly synthetic registered collector."""
import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console.delivery import CustomerTarget, PlanDelivery, RegisteredCustomerSync

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
TARGET = CustomerTarget("env-target", "target-dev", "fixture-cluster", "b" * 40, "c" * 40, "synthetic")
MODULE = dict(environment_id="env-target", environment="target-dev", module="object-store", params={"public": False})


def install(app, state):
    state["sync_document"] = dict(environment="target-dev", expected_revision="b" * 40,
        observed_at=NOW.isoformat(), status="SYNC_VERIFIED", reasons=[], cluster_identity_matches=True,
        synced_resource_count=3, authorizes_cutover=False, migration_verified=False, control_equivalence_verified=False)

    def load(scope, plan, target):
        state["delivery_reads"] = state.get("delivery_reads", 0) + 1
        raw = json.dumps(state["sync_document"]).encode()
        value = RegisteredCustomerSync(plan["plan_id"], plan["plan_digest"], plan["version"],
            target.environment_id, target.cluster_uid, "c" * 40, hashlib.sha256(raw).hexdigest(), raw)
        state.get("during_read", lambda: None)()
        return replace(value, **state.get("change_receipt", {}))

    app.delivery = PlanDelivery(app.api,
        authorize=lambda _s, _p, action: state.get("delivery_allowed", True) and action == "read",
        subject=lambda *_: "fixture-reader", modules=[MODULE], targets=[TARGET],
        load_sync=load, authenticate_sync=lambda *_: state.get("producer_allowed", True), clock=lambda: state.get("now", NOW))


@pytest.fixture
def case(tmp_path):
    app, store, state, _ = setup(tmp_path / "state.db")
    install(app, state)
    return app, store, state


def client(case, authenticated=True):
    return TestClient(case[0], base_url=ORIGIN,
        cookies={"fixture-session": "test-only"} if authenticated else {})


def test_bound_observation_is_not_migration_or_execution_authority(case):
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/delivery")
    assert response.status_code == 200, response.text
    value = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert value["customers"][0]["status"] == "SYNC_VERIFIED"
    assert value["customers"][0]["migration_verified"] is False
    assert value["authorizes_execution"] is False
    assert value["iac"]["configured"] is False  # native IaC API not mounted in this fixture
    assert not any(value["iac"]["actions"].values())
    assert "fixture-cluster" not in response.text
    assert case[2]["submitted"] == 0


@pytest.mark.parametrize("authenticated,key", [(False, None), (True, "delivery_allowed")])
def test_auth_before_any_producer_access(case, authenticated, key):
    if key:
        case[2][key] = False
    with client(case, authenticated) as c:
        assert c.get("/v1/plans/plan-demo/delivery").status_code == 403
    assert not case[2].get("delivery_reads")


@pytest.mark.parametrize("change", [
    {"cluster_uid": "other"}, {"plan_id": "other"}, {"plan_digest": "d" * 64},
    {"plan_version": 99}, {"plan_version": True}, {"environment_id": "other"},
    {"content_sha256": "f" * 64}, {"source_revision": "main"}, {"source_revision": "d" * 40}, {"document": b"{}"},
])
def test_receipt_mismatch_withdraws_success(case, change):
    case[2]["change_receipt"] = change
    with client(case) as c:
        value = c.get("/v1/plans/plan-demo/delivery").json()
    assert value["customers"][0]["status"] == "UNKNOWN"


@pytest.mark.parametrize("change", [
    {"expected_revision": "d" * 40}, {"cluster_identity_matches": False},
    {"authorizes_cutover": True}, {"migration_verified": True}, {"control_equivalence_verified": True},
    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"observed_at": (NOW - timedelta(minutes=5)).isoformat()},
    {"status": "PASS"}, {"status": "SYNC_RETRYING", "retry_count": True},
    {"status": "SYNC_RETRYING", "retry_count": 0}, {"synced_resource_count": 0},
    {"reasons": ["access_token=private-do-not-display"]}, {"credential": "private-do-not-display"},
])
def test_bad_observation_is_bounded_unknown_without_raw_diagnostics(case, change):
    case[2]["sync_document"].update(change)
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/delivery")
    assert response.json()["customers"][0]["status"] == "UNKNOWN"
    assert "private-do-not-display" not in response.text


def test_retry_is_not_success(case):
    case[2]["sync_document"].update(status="SYNC_RETRYING", reasons=["OPERATION_RETRYING"], retry_count=2)
    with client(case) as c:
        result = c.get("/v1/plans/plan-demo/delivery").json()["customers"][0]
    assert result["status"] == "SYNC_RETRYING" and result["migration_verified"] is False


@pytest.mark.parametrize("key", ["allowed", "delivery_allowed", "producer_allowed"])
def test_revoke_while_reading(case, key):
    state = case[2]
    state["during_read"] = lambda: state.update({key: False})
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/delivery")
    if key == "producer_allowed":
        assert response.json()["customers"][0]["status"] == "UNKNOWN"
    else:
        assert response.status_code == 403


def test_only_get_and_no_browser_supplied_registration(case):
    with client(case) as c:
        for suffix in ("?cluster=other", "?revision=main"):
            assert c.get("/v1/plans/plan-demo/delivery" + suffix).status_code == 400
        assert c.post("/v1/plans/plan-demo/delivery", json={}).status_code == 405
        assert c.get("/v1/plans/foreign-plan/delivery").status_code == 403


def test_expiry_during_producer_read(case):
    case[2]["during_read"] = lambda: case[2].update(now=NOW + timedelta(minutes=5))
    with client(case) as c:
        assert c.get("/v1/plans/plan-demo/delivery").json()["customers"][0]["status"] == "UNKNOWN"


def test_existing_get_only_customer_collector_output_flows_without_a_second_evaluator(case):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("customer_test_source", root / "services/iac/tests/test_customer_status.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    calls = []
    def get(kind, name=None, **_kwargs):
        calls.append((kind, name))
        if kind == "namespace":
            return {"metadata": {"uid": TARGET.cluster_uid}}
        if kind == "crd":
            return {"metadata": {"name": name}}
        if kind == "deployments,statefulsets":
            return {"items": fixtures.workloads()}
        return fixtures.application()
    output = fixtures.customer.inspect(get, expected_cluster_uid=TARGET.cluster_uid,
        environment=TARGET.environment, revision=fixtures.REVISION, now=fixtures.NOW)
    case[0].delivery.targets = (replace(TARGET, reviewed_revision=fixtures.REVISION),)
    case[2].update(sync_document=output, now=fixtures.NOW)
    with client(case) as c:
        result = c.get("/v1/plans/plan-demo/delivery").json()["customers"][0]
    assert result["status"] == "SYNC_VERIFIED"
    assert result["synced_resource_count"] == 1
    assert result["migration_verified"] is False
    assert calls[0] == calls[-1] == ("namespace", "kube-system")
