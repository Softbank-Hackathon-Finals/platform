"""Console authority boundary. Chain verifier here is an explicit test double.
Actual T11/T24 compatibility is tested separately by interop_audit.py.
"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console.audit import PlanAudit, RegisteredAuditSnapshot


def document(plan):
    return {
        "records": [
            {
                "plan_id": plan["plan_id"],
                "run_id": "run-test",
                "record_id": "run-test:1",
                "recorded_at": "2026-11-05T01:00:00Z",
                "sha256": "a" * 64,
                "source": "agent.tools",
                "kind": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                "origin_id": "t11:run-test:attempt1:0",
                "payload": {
                    "attempt": 1,
                    "tool": "rehearsal.exec",
                    "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                    "outcome": "DENIED",
                    "category": "bucket_delete",
                    "untrusted_extra": "NEVER_DISPLAY",
                },
            }
        ]
    }


def wire(api, state):
    async def load(scope, plan):
        state["loads"] = state.get("loads", 0) + 1
        doc = document(plan)
        state.get("mutate", lambda _: None)(doc)
        registered = RegisteredAuditSnapshot(
            plan["plan_id"], plan["plan_digest"], "run-test", 1, "a" * 64, json.dumps(doc).encode()
        )
        state.get("during_read", lambda: None)()
        return state.get("envelope", lambda v: v)(registered)

    async def authenticate(scope, plan, registered):
        state["checks"] = state.get("checks", 0) + 1
        return state.get("authority", True) if not state.get("revoke") else state["checks"] == 1

    return PlanAudit(
        api,
        load=load,
        authenticate=authenticate,
        verify_chain=lambda _: (state.get("chain_ok", True), "fixture"),
    )


@pytest.fixture
def fixture(tmp_path):
    app, _, auth, _ = setup(tmp_path / "audit.db")
    state = {}
    app.audit = wire(app.api, state)
    return app, state, auth


def test_projection_preserves_identity_without_payload(fixture):
    app, state, _ = fixture
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        response = c.get("/v1/plans/plan-demo/audit")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        doc = response.json()
        assert doc["rows"][0]["record_id"] == "run-test:1"
        assert doc["rows"][0]["origin_id"] == "t11:run-test:attempt1:0"
        assert not doc["retention_verified"] and not doc["authorizes_cutover"]
        assert "NEVER_DISPLAY" not in response.text and "payload" not in response.text
        assert state["checks"] == 2


def test_auth_before_read_and_no_foreign_plan(fixture):
    app, state, _ = fixture
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/audit").status_code == 403
        c.cookies.set("fixture-session", "test-only")
        assert c.get("/v1/plans/foreign/audit").status_code == 403
        assert not state


@pytest.mark.parametrize(
    "fault",
    [
        "unverified",
        "truthy",
        "revoked",
        "bad_chain",
        "truncated",
        "wrong_plan",
        "wrong_digest",
        "wrong_run",
        "wrong_tail",
        "duplicate_origin",
        "denial",
        "attempt_bool",
        "origin",
        "too_large",
        "session_revoked",
        "loader_error",
    ],
)
def test_fail_closed(fixture, fault):
    app, state, auth = fixture
    if fault == "unverified":
        state["authority"] = False
    if fault == "truthy":
        state["authority"] = 1
    if fault == "revoked":
        state["revoke"] = True
    if fault == "bad_chain":
        state["chain_ok"] = False
    if fault == "truncated":
        state["envelope"] = lambda r: replace(r, record_count=2)
    if fault == "wrong_plan":
        state["envelope"] = lambda r: replace(r, plan_id="other")
    if fault == "wrong_digest":
        state["envelope"] = lambda r: replace(r, plan_digest="f" * 64)
    if fault == "wrong_tail":
        state["envelope"] = lambda r: replace(r, chain_digest="f" * 64)
    if fault == "wrong_run":
        state["mutate"] = lambda d: d["records"][0].update(run_id="foreign")
    if fault == "duplicate_origin":
        state["mutate"] = lambda d: d["records"].append(
            dict(d["records"][0], record_id="run-test:2")
        )
        state["envelope"] = lambda r: replace(r, record_count=2)
    if fault == "denial":
        state["mutate"] = lambda d: d["records"][0]["payload"].update(outcome="ALLOWED")
    if fault == "attempt_bool":
        state["mutate"] = lambda d: d["records"][0]["payload"].update(attempt=True)
    if fault == "origin":
        state["mutate"] = lambda d: d["records"][0].update(origin_id="t11:foreign:attempt1:0")
    if fault == "too_large":
        state["envelope"] = lambda r: replace(r, document=b" " * 2097153)
    if fault == "session_revoked":
        state["during_read"] = lambda: auth.update(allowed=False)
    if fault == "loader_error":

        def bad():
            raise RuntimeError("PRIVATE_STORAGE_ERROR")

        state["during_read"] = bad
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        response = c.get("/v1/plans/plan-demo/audit")
        assert response.status_code == (403 if fault == "session_revoked" else 503)
        assert "PRIVATE" not in response.text and "run-test" not in response.text


def test_unconfigured_is_unknown_not_empty(fixture):
    app, *_ = fixture
    app.audit = None
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/audit").status_code == 503
        assert c.post("/v1/plans/plan-demo/audit").status_code == 405
