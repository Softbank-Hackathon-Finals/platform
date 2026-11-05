"""Actual T10 plan/tenant boundary; report source/issuer are explicit fixtures."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console import ConsoleApp
from bravo_console.reports import CONTRACTS, PlanControlReport, RegisteredGateDocument

ROOT = Path(__file__).resolve().parents[3]


def report_document(plan):
    doc = json.loads((ROOT / "docs/controls/examples/gate-after-fix.json").read_text())
    doc.update(
        gate_id="gate-one",
        context_digest="e" * 64,
        app=plan["workload_id"],
        source_environment_id=plan["source_environment_id"],
        target_environment_id=plan["target_environment_id"],
        evaluated_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    return doc


def wire(api, *, authority=None):
    state = authority if authority is not None else {}

    async def load(scope, plan):
        state["loads"] = state.get("loads", 0) + 1
        doc = state.get("change", lambda d: d)(report_document(plan))
        state.get("during_read", lambda: None)()
        value = RegisteredGateDocument(
            plan["plan_id"], plan["plan_digest"], json.dumps(doc).encode()
        )
        return state.get("envelope", lambda v: v)(value)

    async def authenticate(scope, plan, registered):
        state["auth_calls"] = state.get("auth_calls", 0) + 1
        if state.get("revoke_report_after_first_check"):
            return state["auth_calls"] == 1
        return state.get("verified", True)

    return PlanControlReport(api, load=load, authenticate=authenticate)


@pytest.fixture
def fixture(tmp_path):
    wrapped, store, auth, _ = setup(tmp_path / "reports.db", workload_id="demo-chat")
    state = {}
    reader = wire(wrapped.api, authority=state)
    return ConsoleApp(wrapped.api, public_origin=ORIGIN, controls=reader), state, auth, store


def test_unauthorized_plan_does_not_read_report(fixture):
    app, state, *_ = fixture
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/controls").status_code == 403
        assert state == {}
        c.cookies.set("fixture-session", "test-only")
        assert c.get("/v1/plans/other-tenant/controls").status_code == 403
        assert state == {}


def test_safe_projection_and_both_authority_reads(fixture):
    app, state, *_ = fixture
    state["change"] = lambda d: (
        d | {"rows": [r | {"question": "PRIVATE RAW QUESTION"} for r in d["rows"]]}
    )
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 200, r.text
        value = r.json()
        assert value["authorizes_cutover"] is False
        assert value["decision"] == "PASS" and len(value["rows"]) == 3
        assert "PRIVATE" not in r.text and "question" not in r.text
        assert state["auth_calls"] == 2
        assert r.headers["cache-control"] == "no-store"


def test_report_for_generated_workload_id_is_rendered(tmp_path):
    # T10 registers workloads under generated IDs; the catalog profile names its reference app.
    workload = "workload-" + "0123456789abcdef" * 2
    wrapped, *_ = setup(tmp_path / "reports.db", workload_id=workload)
    state = {}
    app = ConsoleApp(wrapped.api, public_origin=ORIGIN, controls=wire(wrapped.api, authority=state))
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 200, r.text
        example = json.loads((ROOT / "docs/controls/examples/gate-after-fix.json").read_text())
        assert r.json()["profile"] == example["profile"]
        state["change"] = lambda d: d | {"app": "demo-chat"}  # the catalog reference app is not this plan's app
        assert c.get("/v1/plans/plan-demo/controls").status_code != 200


@pytest.mark.parametrize(
    "change",
    ["app", "source", "target", "profile", "missing", "duplicate", "decision", "future", "control"],
)
def test_wrong_scope_partial_or_contradictory_report_not_rendered(fixture, change):
    app, state, *_ = fixture

    def bad(doc):
        if change == "app":
            doc["app"] = "other-app"
        elif change == "source":
            doc["source_environment_id"] = "other-source"
        elif change == "target":
            doc["target_environment_id"] = "other-target"
        elif change == "profile":
            doc["profile"]["version"] = "999"
        elif change == "missing":
            doc["rows"].pop()
        elif change == "duplicate":
            doc["rows"][1] = doc["rows"][0]
        elif change == "decision":
            doc["rows"][0].update(decision="BLOCK", reason="TARGET_FAIL", target_verdict="FAIL")
        elif change == "future":
            doc["evaluated_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        elif change == "control":
            doc["rows"][0]["control_id"] = "AUD-01"
        return doc

    state["change"] = bad
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 503
        assert "rows" not in r.text


@pytest.mark.parametrize(
    "change", ["plan", "digest", "body", "large", "duplicate-json", "nonfinite", "untrusted"]
)
def test_unverified_envelope_or_hostile_json_does_not_escape(fixture, change):
    app, state, *_ = fixture
    if change == "untrusted":
        state["verified"] = "truthy is not verified"
    else:

        def bad(value):
            if change == "plan":
                return replace(value, plan_id="other-plan")
            if change == "digest":
                return replace(value, plan_digest="f" * 64)
            if change == "body":
                return replace(value, document=b"invalid PRIVATE error")
            if change == "large":
                return replace(value, document=b"x" * 262145)
            if change == "duplicate-json":
                return replace(value, document=value.document[:-1] + b', "decision":"PASS"}')
            return replace(value, document=value.document[:-1] + b', "private":NaN}')

        state["envelope"] = bad
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 503
        assert "PRIVATE" not in r.text and "rows" not in r.text


def test_revocation_during_read_discards_report(fixture):
    app, state, auth, _ = fixture
    state["during_read"] = lambda: auth.update(allowed=False)
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 403 and "rows" not in r.text


def test_event_change_during_read_discards_report(fixture):
    app, state, _, store = fixture

    def mutate():
        store.append(
            plan_id="plan-demo",
            run_id="changed",
            event_type="gate.completed",
            data=dict(
                gate_id="new-gate",
                context_digest="f" * 64,
                decision="BLOCK",
                evidence_ref="new-evidence",
            ),
            occurred_at=datetime.now(UTC).isoformat(),
            producer_key="changed",
        )

    state["during_read"] = mutate
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 503 and "rows" not in r.text


def test_report_authority_revoked_after_projection_is_not_exposed(fixture):
    app, state, *_ = fixture
    state["revoke_report_after_first_check"] = True
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 503 and "rows" not in r.text
        assert state["auth_calls"] == 2


def test_bundle_matches_reviewed_t5_source():
    provenance = json.loads((CONTRACTS / "provenance.json").read_text())
    for name, digest in provenance["sha256"].items():
        assert (CONTRACTS / name).read_bytes() == (ROOT / "docs/controls" / name).read_bytes()
        assert hashlib.sha256((CONTRACTS / name).read_bytes()).hexdigest() == digest
