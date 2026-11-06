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


def report_document(plan, phase="initial"):
    doc = json.loads((ROOT / "docs/controls/examples/gate-after-fix.json").read_text())
    doc.update(
        gate_id="gate-one",
        context_digest="e" * 64,
        app=plan["workload_id"],
        source_environment_id=plan["source_environment_id"],
        target_environment_id=plan["target_environment_id"],
        evaluated_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    if phase == "post_apply":
        # Synthetic report with the canonical recheck control set. The trusted
        # envelope supplies the phase independently of this document's profile.
        doc["profile"] = dict(id="apply-recheck", version="1")
        for row in doc["rows"]:
            if row["test_id"] == "AUD-01.1":
                row.update(test_id="ENC-01.1", control_id="ENC-01")
    return doc


def wire(api, *, authority=None):
    state = authority if authority is not None else {}

    async def load(scope, plan):
        state["loads"] = state.get("loads", 0) + 1
        phase = state.get("phase", "initial")
        doc = state.get("change", lambda d: d)(report_document(plan, phase))
        state.get("during_read", lambda: None)()
        value = RegisteredGateDocument(
            plan["plan_id"], plan["plan_digest"], json.dumps(doc).encode(),
            phase, state.get("application_id"),
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
    wrapped, store, auth, _ = setup(
        tmp_path / "reports.db", workload_id="demo-chat", profile=("demo-gate", "1")
    )
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
        snapshot = c.get("/v1/plans/plan-demo").json()
        assert value["plan_version"] == snapshot["version"]
        assert value["plan_state"] == snapshot["state"]
        assert value["decision"] == "PASS" and len(value["rows"]) == 3
        assert "PRIVATE" not in r.text and "question" not in r.text
        assert state["auth_calls"] == 2
        assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", ["none", "short", "long", "expired", "current", "naive", "text", "boolean"])
def test_read_projection_checks_deadline_type_and_does_not_extend_it(fixture, kind):
    app, state, *_ = fixture
    now = datetime.now(UTC)
    deadline = {
        "none": None, "short": now + timedelta(seconds=2),
        "long": now + timedelta(hours=1), "expired": now - timedelta(seconds=1),
        "current": now, "naive": datetime.now(), "text": now.isoformat(), "boolean": True,
    }[kind]
    app.controls.clock = lambda: now
    state["envelope"] = lambda value: replace(value, authority_expires_at=deadline)
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get("/v1/plans/plan-demo/controls")
    if kind in {"none", "short", "long"}:
        assert response.status_code == 200
        expected = min(now + timedelta(seconds=15), deadline) if deadline else now + timedelta(seconds=15)
        assert datetime.fromisoformat(response.json()["read_valid_until"]) == expected
        assert response.json()["authorizes_cutover"] is False
    else:
        assert response.status_code == 503 and "rows" not in response.text


@pytest.mark.parametrize("change", ["expired", "clock-regressed"])
def test_final_read_check_rejects_expiry_or_clock_regression_after_projection(fixture, change):
    app, state, *_ = fixture
    now = datetime.now(UTC)
    clock = {"now": now}
    app.controls.clock = lambda: clock["now"]
    state["envelope"] = lambda value: replace(value, authority_expires_at=now + timedelta(seconds=2))
    original = app.controls.authenticate

    async def finish(scope, plan, registered):
        result = await original(scope, plan, registered)
        if state["auth_calls"] == 2:
            clock["now"] = now + timedelta(seconds=2) if change == "expired" else now - timedelta(microseconds=1)
        return result

    app.controls.authenticate = finish
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get("/v1/plans/plan-demo/controls")
    assert response.status_code == 503 and "rows" not in response.text


def test_report_for_generated_workload_id_is_rendered(tmp_path):
    # T10 registers workloads under generated IDs; the catalog profile names its reference app.
    workload = "workload-" + "0123456789abcdef" * 2
    wrapped, *_ = setup(tmp_path / "reports.db", workload_id=workload, profile=("demo-gate", "1"))
    state = {}
    app = ConsoleApp(wrapped.api, public_origin=ORIGIN, controls=wire(wrapped.api, authority=state))
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        r = c.get("/v1/plans/plan-demo/controls")
        assert r.status_code == 200, r.text
        assert r.json()["profile"] == dict(id="demo-gate", version="1")
        state["change"] = lambda d: d | {"app": "demo-chat"}  # the catalog reference app is not this plan's app
        assert c.get("/v1/plans/plan-demo/controls").status_code != 200


@pytest.mark.parametrize("profile", [("full", "1"), ("demo-gate", "2"), None])
def test_plan_profile_cannot_be_replaced_by_valid_smaller_report(tmp_path, profile):
    wrapped, *_ = setup(tmp_path / "profile.db", workload_id="demo-chat", profile=profile)
    app = ConsoleApp(wrapped.api, public_origin=ORIGIN, controls=wire(wrapped.api))
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        # The issuer and plan digest match, and all three demo tests pass. That
        # still cannot satisfy a full/version-different/unbound plan's display.
        response = client.get("/v1/plans/plan-demo/controls")
        assert response.status_code == 503
        assert "rows" not in response.json()
        assert response.json()["detail"]["code"] == "control_report_profile_mismatch"


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


@pytest.mark.parametrize("fault", [None, "missing-app", "invalid-app", "initial", "legacy", "wrong-profile"])
def test_post_apply_report_is_bound_to_trusted_phase_and_recheck_profile(tmp_path, fault):
    wrapped, *_ = setup(tmp_path / "post-apply.db", workload_id="demo-chat",
                         profile=("demo-gate", "1"),
                         recheck=None if fault == "legacy" else ("apply-recheck", "1"))
    state = {"phase": "post_apply", "application_id": "applied-one"}
    if fault == "missing-app":
        state["application_id"] = None
    if fault == "invalid-app":
        state["application_id"] = "../invalid"
    if fault == "initial":
        state["envelope"] = lambda value: replace(value, gate_phase="initial", application_id=None)
    if fault == "wrong-profile":
        state["change"] = lambda doc: doc | {"profile": dict(id="demo-gate", version="1")}
    app = ConsoleApp(wrapped.api, public_origin=ORIGIN, controls=wire(wrapped.api, authority=state))
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get("/v1/plans/plan-demo/controls")
        if fault:
            assert response.status_code == 503
            assert "rows" not in response.json()
        else:
            assert response.status_code == 200, response.text
            assert response.json()["profile"] == dict(id="apply-recheck", version="1")
            assert response.json()["gate_phase"] == "post_apply"
            assert response.json()["application_id"] == "applied-one"
            assert response.json()["authorizes_cutover"] is False
            assert state["auth_calls"] == 2
