"""T45 HTTP API: plans for anyone authenticated, runs and confirmations for humans only."""

import time

import pytest
from fastapi.testclient import TestClient
from test_executor import REQUEST, world  # noqa: F401  (fixture)

from bravo_infra_plan.adapters import FilePlanStore
from bravo_infra_plan.api import Principal, create_app

PEOPLE = {"human-token": Principal("operator-jeon", "human", "human-token"),
          "agent-token": Principal("agent-run-7", "agent", "agent-token")}


@pytest.fixture
def client(world):  # noqa: F811 (fixture)
    app = create_app(stack=world["executor"]().stack, plans=FilePlanStore(world["tmp"] / "plans"),
                     executor_for=lambda token: world["executor"](),
                     resolve_principal=lambda r: PEOPLE.get(r.headers.get("authorization", "")[7:]))
    return TestClient(app), world


def as_(who):
    return {"Authorization": f"Bearer {who}-token"}


def wait_for(client, run_id, statuses):
    for _ in range(200):
        run = client.get(f"/v1/infra/runs/{run_id}", headers=as_("human"))
        if run.status_code == 200 and run.json()["status"] in statuses:
            return run.json()
        time.sleep(0.05)
    raise AssertionError(run.text)


def test_an_agent_plans_and_a_human_runs_the_approved_digest(client):
    http, state = client
    assert http.get("/v1/infra/request-schema").status_code == 401
    planned = http.post("/v1/infra/plans", json=REQUEST, headers=as_("agent"))
    assert planned.status_code == 201, planned.text
    plan = planned.json()
    assert plan["binding"]["requested_by"] == "agent-run-7" and len(plan["plan_digest"]) == 64
    assert {a["id"] for a in plan["operator_actions"]} >= {"host-dirs", "openbao-init"}
    assert http.get(f"/v1/infra/plans/{plan['infra_plan_id']}", headers=as_("human")).json() == plan

    body = {"migration_plan_id": "plan-demo", "approval_id": "appr-1"}
    route = f"/v1/infra/plans/{plan['infra_plan_id']}/runs"
    refused = http.post(route, json=body, headers=as_("agent") | {"Idempotency-Key": "k1"})
    assert (refused.status_code, refused.json()) == (403, {"detail": {"code": "human_required"}})

    state["approvals"].approve("appr-1", plan["plan_digest"])  # T10 approval_kind=infra stands in here
    started = http.post(route, json=body, headers=as_("human") | {"Idempotency-Key": "k1"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    run = wait_for(http, run_id, {"WAITING_OPERATOR"})
    assert run["started_by"] == "operator-jeon"
    confirm = f"/v1/infra/runs/{run_id}/steps/host-dirs/confirm"
    assert http.post(confirm, headers=as_("agent")).status_code == 403
    state["cluster"].prepare_node()  # the platform prepared the node; the confirmation re-observes it
    assert http.post(confirm, headers=as_("human")).status_code == 202
    assert wait_for(http, run_id, {"SUCCEEDED", "HOLD", "FAILED"})["status"] == "SUCCEEDED"


def test_a_plan_that_no_longer_matches_its_approval_is_refused(client):
    http, state = client
    plan = http.post("/v1/infra/plans", json=REQUEST, headers=as_("human")).json()
    state["approvals"].approve("appr-1", "e" * 64)
    route = f"/v1/infra/plans/{plan['infra_plan_id']}/runs"
    run_id = http.post(route, json={"migration_plan_id": "plan-demo", "approval_id": "appr-1"},
                       headers=as_("human") | {"Idempotency-Key": "k1"}).json()["run_id"]
    run = wait_for(http, run_id, {"REFUSED"})
    assert run["reason"] == "plan_digest_mismatch"


def test_bad_requests(client):
    http, _ = client
    bad = dict(REQUEST, params=dict(REQUEST["params"], node_name="Bad_Node"))
    assert http.post("/v1/infra/plans", json=bad, headers=as_("human")).status_code == 422
    assert http.get("/v1/infra/plans/infra-missing", headers=as_("human")).status_code == 404
    assert http.get("/v1/infra/runs/run-missing", headers=as_("human")).status_code == 404
