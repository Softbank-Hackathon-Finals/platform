"""Display contract and real T10 plan/session checks; no cloud mutation."""

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console.workload import PlanWorkload, RegisteredWorkload, validate_projection

NOW = datetime(2026, 11, 7, tzinfo=UTC)
SOURCE = "a" * 40


def document():
    return json.loads((Path(__file__).parent / "fixtures/workload-synthetic.json").read_text())


def install(app, state, *, clock=lambda: NOW):
    def load(_scope, plan):
        state["workload_reads"] = state.get("workload_reads", 0) + 1
        doc = document()
        doc.update(
            plan_id=plan["plan_id"], plan_digest=plan["plan_digest"], plan_version=plan["version"]
        )
        state.get("change_workload", lambda _: None)(doc)
        raw = json.dumps(doc).encode()
        value = RegisteredWorkload("fixture-workload", SOURCE, hashlib.sha256(raw).hexdigest(), raw)
        return state.get("change_registration", lambda x: x)(value)

    app.workload = PlanWorkload(
        app.api,
        load=load,
        authorize=lambda *_: state.get("workload_allowed", True),
        authenticate=lambda *_: state.get("producer_allowed", True),
        producer_id="fixture-workload",
        source_revision=SOURCE,
        provenance="synthetic",
        clock=clock,
    )
    return app.workload


@pytest.fixture
def case(tmp_path):
    app, store, state, *_ = setup(tmp_path / "state.db")
    install(app, state)
    return app, store, state


def client(case):
    return TestClient(case[0], base_url=ORIGIN, cookies={"fixture-session": "test-only"})


def test_real_t10_binding_and_read_only_response(case):
    with client(case) as c:
        snapshot = c.get("/v1/plans/plan-demo").json()
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 200, response.text
    value = response.json()
    assert (value["plan_id"], value["plan_digest"], value["plan_version"]) == (
        snapshot["plan_id"],
        snapshot["plan_digest"],
        snapshot["version"],
    )
    assert value["authorizes_execution"] is False
    assert value["provenance"] == "synthetic"
    assert response.headers["cache-control"] == "no-store"
    assert case[2]["submitted"] == 0


def test_missing_session_never_calls_producer(case):
    with TestClient(case[0], base_url=ORIGIN) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code in (401, 403)
    assert not case[2].get("workload_reads")


@pytest.mark.parametrize("key", ["workload_allowed", "producer_allowed"])
def test_separate_read_permission_and_producer_authority(case, key):
    case[2][key] = False
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == (403 if key == "workload_allowed" else 503)
    assert "synthetic-api" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": "b" * 40},
        {"producer_id": "another"},
        {"content_sha256": "f" * 64},
        {"document": b"{}"},
    ],
)
def test_unregistered_or_modified_producer_is_rejected(case, change):
    case[2]["change_registration"] = lambda v: replace(v, **change)
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "workload_unavailable_or_unverified"


@pytest.mark.parametrize(
    "fault",
    [
        "plan",
        "version",
        "digest",
        "live",
        "credential",
        "extra",
        "uid",
        "proof",
        "sequence",
        "future",
        "boolean_revision",
        "oversize",
        "group_proof",
        "resource_collision",
        "unknown_reference",
    ],
)
def test_invalid_projection_does_not_escape(case, fault):
    def mutate(d):
        if fault == "plan":
            d["plan_id"] = "other-plan"
        elif fault == "version":
            d["plan_version"] += 1
        elif fault == "digest":
            d["plan_digest"] = "f" * 64
        elif fault == "live":
            d["provenance"] = "live"
        elif fault == "credential":
            d["nodes"][0]["detail"] = "password=fixture-only-not-a-real-password"
        elif fault == "extra":
            d["command"] = "not-a-display-field"
        elif fault == "uid":
            d["nodes"][1]["resource_uid"] = "replacement-pod"
        elif fault == "proof":
            d["responses"][3]["evidence_id"] = "demo-stage-approve"
        elif fault == "sequence":
            d["events"][1]["sequence"] = 1
        elif fault == "future":
            d["observed_at"] = "2099-01-01T00:00:00Z"
        elif fault == "boolean_revision":
            d["revision"] = True
        elif fault == "group_proof":
            d["responses"][3]["node_ids"].append("database")
        elif fault == "resource_collision":
            d["edges"][0]["id"] = d["nodes"][0]["id"]
        elif fault == "unknown_reference":
            d["nodes"][0]["health"] = "unknown"
            d["nodes"][0]["health_evidence"] = "absent"
        elif fault == "oversize":
            d["source_label"] = "x" * 120001

    case[2]["change_workload"] = mutate
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 503
    assert "password" not in response.text and "synthetic-api" not in response.text


def test_revocation_during_read_drops_in_flight_document(case):
    original = case[0].workload.load

    def revoked(scope, plan):
        result = original(scope, plan)
        case[2]["allowed"] = False
        return result

    case[0].workload.load = revoked
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code in (401, 403)
    assert "synthetic-api" not in response.text


def test_plan_change_during_read_drops_in_flight_document(case):
    original = case[0].workload.load

    def changed(scope, plan):
        result = original(scope, plan)
        with case[1]._transaction(write=True, plan_id="plan-demo") as c:
            c.execute("UPDATE plans SET version=version+1 WHERE plan_id='plan-demo'")
        return result

    case[0].workload.load = changed
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 503


def test_producer_authority_is_rechecked_after_read(case):
    count = 0

    def auth(*_):
        nonlocal count
        count += 1
        return count == 1

    case[0].workload.authenticate = auth
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 503 and count == 2


def test_provider_timeout_returns_no_partial_result(case):
    async def slow(*_):
        await asyncio.sleep(10)

    case[0].workload.load = slow
    case[0].workload.timeout = 0.05
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code == 503


def test_timed_out_blocking_read_keeps_capacity_until_it_actually_exits(case):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = case[0].workload.load

    def slow(scope, plan):
        started.set()
        try:
            release.wait(3)
            return original(scope, plan)
        finally:
            finished.set()

    case[0].workload.load = slow
    case[0].workload.timeout = 0.05
    case[0].workload.capacity = threading.BoundedSemaphore(1)
    with client(case) as c:
        try:
            first = c.get("/v1/plans/plan-demo/workload")
            assert started.is_set() and first.status_code == 503
            second = c.get("/v1/plans/plan-demo/workload")
            assert second.status_code == 503
            assert second.json()["detail"]["code"] == "workload_busy"
            assert not finished.is_set()
        finally:
            release.set()
            assert finished.wait(3)


@pytest.mark.parametrize(
    "method,path,headers,expected",
    [
        ("post", "/v1/plans/plan-demo/workload", {}, 405),
        ("get", "/v1/plans/plan-demo/workload?source=arbitrary", {}, 400),
        ("get", "/v1/plans/plan-demo/workload", {"Origin": "https://other.invalid"}, 403),
    ],
)
def test_browser_cannot_submit_or_select_a_producer(case, method, path, headers, expected):
    with client(case) as c:
        response = getattr(c, method)(path, headers=headers)
    assert response.status_code == expected
    assert not case[2].get("workload_reads")


def test_unconfigured_is_not_a_healthy_empty_graph(case):
    case[0].workload = None
    with client(case) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert (
        response.status_code == 503 and response.json()["detail"]["code"] == "workload_unconfigured"
    )


def test_shared_js_fixture_has_exact_python_semantics():
    d = document()
    plan = {"plan_id": d["plan_id"], "plan_digest": d["plan_digest"], "version": d["plan_version"]}
    assert validate_projection(d, plan, NOW) == d


def test_existing_security_snapshot_adapter_fixture_is_consumable_without_success_inference():
    d = json.loads((Path(__file__).parent / "fixtures/security-projection.json").read_text())
    plan = {"plan_id": d["plan_id"], "plan_digest": d["plan_digest"], "version": d["plan_version"]}
    value = validate_projection(d, plan, NOW)
    assert len(value["nodes"]) == 20
    assert all(n["health"] == "unknown" for n in value["nodes"])
    assert all(e["state"] == "unknown" for e in value["edges"])
    assert all(r["status"] == "unknown" for r in value["responses"])
    assert sum(n["security"] == "warning" for n in value["nodes"]) == 2
