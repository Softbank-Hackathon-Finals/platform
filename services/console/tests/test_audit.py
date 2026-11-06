"""Console authority boundary over the real T24 writer, registration and public reader.

Records are written by T24's RunLedger + FileSink from T11-shaped tool events and read back with
read_local_run against a receipt-built registration. A local FileSink keeps no versions, so the
overwrite/delete-marker checks of read_run are T24's own tests.
"""

import json
from dataclasses import replace

import pytest
from bravo_audit.adapters import from_agent_tool_event
from bravo_audit.ledger import RunLedger
from bravo_audit.registration import registration_from_receipt
from bravo_audit.sink import FileSink
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console.audit import MAX_RECORDS, PlanAudit, RegisteredAuditRun, t24_reader

RUN = "run-test"


def tool_event(tool, attempt=1, outcome="DENIED", reason="DESTRUCTIVE_ATTEMPT_BLOCKED", **metadata):
    """What T11 Tools.record emits: code-owned labels only, never the command."""
    return {"run_id": RUN, "attempt": attempt, "tool": tool, "outcome": outcome, "reason": reason, **metadata}


class Run:
    def __init__(self, root, plan_id):
        self.sink, self.ledger, self.plan_id = FileSink(root), RunLedger(plan_id, RUN), plan_id
        self.receipt, self.index = None, 0

    def tool(self, event):
        _, _, source, kind, origin, payload = from_agent_tool_event(event, self.plan_id, self.index)
        self.append(source, kind, payload, origin)
        self.index += 1

    def append(self, source, kind, payload, origin):
        record, created = self.ledger.append(source, kind, payload, origin)
        assert created
        self.receipt = self.sink.put(record).as_dict()

    def registration(self):
        return registration_from_receipt(self.plan_id, RUN, self.receipt, "2026-11-07T00:00:00Z")

    def path(self, sequence):
        return self.sink.root / "audit" / self.plan_id / RUN / f"{sequence:08d}.json"


@pytest.fixture
def fixture(tmp_path):
    app, store, auth, _ = setup(tmp_path / "audit.db")
    plan = store.snapshot("plan-demo")
    run = Run(tmp_path / "stored", "plan-demo")
    run.tool(tool_event("rehearsal.exec", category="bucket_delete"))
    run.tool(tool_event("sandbox.exec", attempt=2, category="bucket_delete"))  # T11 sandbox shell (#270 P1)
    run.tool(tool_event("repo.read", outcome="ALLOWED", reason="ALLOWED"))
    state = {"registered": RegisteredAuditRun("plan-demo", plan["plan_digest"], run.registration())}

    async def load(scope, snapshot):
        state["loads"] = state.get("loads", 0) + 1
        state.get("during_read", lambda: None)()
        return state["registered"]

    async def authenticate(scope, snapshot, registered):
        state["checks"] = state.get("checks", 0) + 1
        return state.get("authority", True) if not state.get("revoke") else state["checks"] == 1

    app.audit = t24_reader(app.api, load=load, authenticate=authenticate, root=run.sink.root)
    return app, store, state, auth, run


def get(app):
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        return c.get("/v1/plans/plan-demo/audit")


def test_projection_preserves_identity_without_payload(fixture):
    app, _, state, _, run = fixture
    response = get(app)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    doc = response.json()
    assert [r["tool"] for r in doc["rows"]] == ["rehearsal.exec", "sandbox.exec"]
    assert doc["rows"][1]["origin_id"] == f"t11:{RUN}:attempt2:1"
    assert (doc["record_count"], doc["tool_records"], doc["rows_total"]) == (3, 3, 2)
    assert doc["chain_digest"] == run.receipt["sha256"]
    assert not doc["retention_verified"] and not doc["authorizes_cutover"]
    assert "payload" not in response.text and "metadata" not in response.text
    assert state["checks"] == 2


def test_a_tool_outside_the_t11_rule_is_unknown_on_its_own_row(fixture):
    app, store, state, _, run = fixture
    run.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED",
               {"attempt": 3, "tool": "Shell Exec", "outcome": "DENIED", "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                "category": "bucket_delete"}, f"t11:{RUN}:attempt3:0")
    state["registered"] = replace(state["registered"], registration=run.registration())
    response = get(app)
    assert response.status_code == 200, response.text
    assert [r["tool"] for r in response.json()["rows"]] == ["rehearsal.exec", "sandbox.exec", None]


def test_no_tool_record_is_reported_as_such_not_as_zero_blocks(fixture, tmp_path):
    app, store, state, _, _ = fixture
    run = Run(tmp_path / "platform-only", "plan-demo")
    run.append("platform.events", "state.changed", {"event_type": "state.changed"}, "t10:event-1")
    state["registered"] = replace(state["registered"], registration=run.registration())
    app.audit = t24_reader(app.api, load=app.audit.load, authenticate=app.audit.authenticate, root=run.sink.root)
    doc = get(app).json()
    assert (doc["tool_records"], doc["rows_total"], doc["rows"]) == (0, 0, [])


def test_a_run_that_grew_shows_only_what_its_registration_vouches_for(fixture):
    app, _, state, _, run = fixture
    earlier = run.registration()
    run.tool(tool_event("rehearsal.exec", attempt=4, category="bucket_delete"))
    state["registered"] = replace(state["registered"], anchors=((1, run.ledger.records[0].sha256),
                                                                 (3, earlier["chain_digest"])))
    doc = get(app).json()
    assert (doc["record_count"], doc["unregistered_records"], doc["rows_total"]) == (3, 1, 2)


def test_the_plan_moving_on_during_the_read_is_not_a_failure(fixture):
    app, store, state, _, _ = fixture

    def advance():
        with store._transaction(write=True, plan_id="plan-demo") as c:
            c.execute("UPDATE plans SET version=version+1 WHERE plan_id='plan-demo'")

    state["during_read"] = advance
    assert get(app).status_code == 200


def test_auth_before_read_and_no_foreign_plan(fixture):
    app, _, state, _, _ = fixture
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/audit").status_code == 403
        c.cookies.set("fixture-session", "test-only")
        assert c.get("/v1/plans/foreign/audit").status_code == 403
        assert c.get("/v1/plans/plan-demo/audit?run=other").status_code == 400
    assert "loads" not in state


@pytest.mark.parametrize(
    "fault",
    [
        "unverified", "truthy", "revoked", "tampered", "wrong_plan", "wrong_digest", "wrong_run",
        "beyond_stored", "wrong_tail", "rewritten", "bad_anchor", "denial", "attempt_bool", "origin",
        "too_large", "not_registered", "session_revoked", "loader_error",
    ],
)
def test_fail_closed(fixture, fault):
    app, _, state, auth, run = fixture
    registered = state["registered"]
    reg = dict(registered.registration)

    def registration(**changes):
        state["registered"] = replace(registered, registration=reg | changes)

    if fault == "unverified":
        state["authority"] = False
    if fault == "truthy":
        state["authority"] = 1
    if fault == "revoked":
        state["revoke"] = True
    if fault == "tampered":
        record = json.loads(run.path(1).read_text())
        record["payload"]["tool"] = "unlisted"
        run.path(1).write_text(json.dumps(record))
    if fault == "wrong_plan":
        state["registered"] = replace(registered, plan_id="other")
    if fault == "wrong_digest":
        state["registered"] = replace(registered, plan_digest="f" * 64)
    if fault == "wrong_run":
        registration(run_id="run-other")
    if fault == "beyond_stored":
        registration(record_count=4, last_sequence=4)
    if fault == "wrong_tail":
        registration(chain_digest="f" * 64)
    if fault == "rewritten":
        state["registered"] = replace(registered, anchors=((2, "f" * 64),))
    if fault == "bad_anchor":
        state["registered"] = replace(registered, anchors=((9, reg["chain_digest"]),))
    if fault in ("denial", "attempt_bool", "origin"):
        payload = {"attempt": 3, "tool": "rehearsal.exec", "outcome": "DENIED",
                   "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}
        origin = f"t11:{RUN}:attempt3:0"
        if fault == "denial":
            payload["outcome"] = "ALLOWED"
        if fault == "attempt_bool":
            payload["attempt"] = True
        if fault == "origin":
            origin = "t11:foreign:attempt3:0"
        run.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", payload, origin)
        state["registered"] = replace(registered, registration=run.registration())
    if fault == "too_large":
        registration(record_count=MAX_RECORDS + 1, last_sequence=MAX_RECORDS + 1)
    if fault == "not_registered":
        state["registered"] = registered.registration  # a bare document, not the host's registration
    if fault == "session_revoked":
        state["during_read"] = lambda: auth.update(allowed=False)
    if fault == "loader_error":

        def bad():
            raise RuntimeError("PRIVATE_STORAGE_ERROR")

        state["during_read"] = bad
    response = get(app)
    assert response.status_code == (403 if fault == "session_revoked" else 503), response.text
    assert "PRIVATE" not in response.text and RUN not in response.text


@pytest.mark.parametrize("answer", [
    lambda run: replace(run, registered=False),
    lambda run: replace(run, chain_digest="f" * 64),
    lambda run: replace(run, records=run.records[:-1]),
    lambda run: replace(run, run_id="run-other"),
])
def test_a_reader_answer_that_does_not_match_the_registration_is_refused(fixture, answer):
    # T24 read_run already anchors the run; the console checks its answer again.
    app, *_ = fixture
    read = app.audit.read_run
    app.audit = PlanAudit(app.api, load=app.audit.load, authenticate=app.audit.authenticate,
                          read=lambda *args: answer(read(*args)))
    assert get(app).status_code == 503


def test_unconfigured_is_unknown_not_empty(fixture):
    app, *_ = fixture
    app.audit = None
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/audit").status_code == 503
        assert c.post("/v1/plans/plan-demo/audit").status_code == 405


def test_one_store_is_required(fixture):
    app, *_ = fixture
    with pytest.raises(ValueError, match="one_audit_store_required"):
        t24_reader(app.api, load=app.audit.load, authenticate=app.audit.authenticate)
    with pytest.raises(ValueError, match="one_audit_store_required"):
        t24_reader(app.api, load=app.audit.load, authenticate=app.audit.authenticate, root="/x",
                   client=object(), bucket="b")
