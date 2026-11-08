"""Current human grants and one actual signed T15 store; cluster/model are fixtures."""

import time
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_change_intake import IntakeFixture
from test_cutover import EXPIRES, NOW
from test_operator_access import Identity
from test_operator_access import signing_key as signing_key
from test_t10_recorded_flow import SPEC
from test_t10_recorded_flow import flow as flow

from bravo_platform_events.change import change_key
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.store import EventError
from bravo_platform_events.trusted_rehearsal import (
    AttemptDispatcher,
    OperatorHoldPolicy,
    TrustedHostPorts,
    build_trusted_host,
)


@pytest.fixture
def managed(backend, signing_key):
    f = IntakeFixture(backend, signing_key)
    human = Identity(signing_key)
    access = OperatorAccess(backend.store, clock=lambda: NOW)
    f.intake.authorize_hold = OperatorHoldPolicy(access, human.verifier)
    return f, human, access


def grant(managed, permissions, revision=0):
    f, human, access = managed
    return access.set_permissions("plan-fixture", human.principal(), permissions=permissions,
        valid_after=NOW, expires_at=EXPIRES, expected_revision=revision,
        administration_ref="management-decision", command_key=f"grant-{revision}")


def test_hold_requires_separate_current_human_grant_and_records_actor(managed):
    f, human, access = managed
    grant(managed, ["read", "approve_change", "start_rehearsal"])
    for credential in (f.token, human.token(), "invalid"):
        with pytest.raises(EventError, match="forbidden"):
            f.hold(credential)
    grant(managed, ["read", "hold_rehearsal"], 1)
    first = f.hold(human.token())
    assert f.hold(human.token()) == first
    with f.backend.store._transaction() as c:
        row = c.execute("SELECT request_json FROM plan_commands WHERE plan_id=? AND command_key=?",
            ("plan-fixture", change_key("hold-decision", "decision-1"))).fetchone()
    assert human.principal().subject_id in row["request_json"]
    assert human.token() not in row["request_json"]
    grant(managed, [], 2)
    with pytest.raises(EventError, match="forbidden"):
        f.hold(human.token())


def test_hold_token_expiry_scope_conflict_and_write_failure(managed):
    f, human, access = managed
    grant(managed, ["read", "hold_rehearsal"])
    with f.backend.reject_insert("plan_commands"), pytest.raises(f.backend.storage_error):
        f.hold(human.token())
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("change-hold-decision-%",)).fetchone()["n"] == 0
    f.hold(human.token())
    with pytest.raises(EventError, match="command_key_conflict"):
        f.intake.hold(human.token(), "plan-fixture", "another-verification", decision_ref="decision-1")
    human.time += 300
    with pytest.raises(EventError, match="forbidden"):
        f.hold(human.token())


def test_one_host_uses_signed_original_for_http_rehearsal_and_approval(flow, signing_key):
    human = Identity(signing_key)
    now = int(time.time())
    human.time = now
    human.verifier.subjects = {"human-1": SPEC.tenant_id}
    token = human.token(iat=now, nbf=now, exp=now+300, auth_time=now)
    actor = human.verifier.verify(token)
    ports = TrustedHostPorts(SimpleNamespace(submit=lambda *_: None, read=lambda *_: None),
        {"env-aws": "source", "env-onprem": "target"}, flow["service"].host.catalog,
        flow["planner"], lambda *_a, **_k: (_ for _ in ()).throw(EventError("authority_unavailable")))
    host = build_trusted_host(flow["store"], ports=ports, records=flow["records"],
        sender=flow["intake"].sender, verifier=human.verifier, token_supplier=lambda: flow["token"])
    host.access.set_permissions(SPEC.plan_id, actor, permissions=["read", "start_rehearsal", "approve_change"],
        valid_after=datetime.fromtimestamp(now-1, UTC).isoformat(),
        expires_at=(datetime.now(UTC)+timedelta(minutes=5)).isoformat(),
        expected_revision=0, administration_ref="approved-grant", command_key="grant")
    assert host.evidence.store is flow["records"] and host.rehearsals.evidence is host.evidence
    assert flow["planner"].events == []  # Assembly does not submit or mint a PASS.
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "start"}
    with TestClient(host.app, base_url="https://host.invalid") as client:
        started = client.post(f"/v1/plans/{SPEC.plan_id}/rehearsals", headers=headers,
            json={"task_id": "bucket", "attempt": 1, "expected_version": 1})
        assert started.status_code == 202, started.text
        receipt = started.json()
        for _ in range(100):
            response = client.post(f"/v1/plans/{SPEC.plan_id}/rehearsals/{receipt['rehearsal_id']}/attempts/1/reconcile",
                                   headers=headers)
            if response.status_code == 409:
                # Signed records may be unreadable while the worker writes.
                # Only read the same attempt again; never start another one.
                assert response.json()["detail"]["code"] == "rehearsal_verification_unavailable"
                time.sleep(0.01)
                continue
            assert response.status_code == 200, response.text
            if response.json()["status"] == "PASS":
                break
            time.sleep(0.01)
        assert response.status_code == 200 and response.json()["status"] == "PASS", response.text
        with flow["store"]._transaction() as c:
            plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
            current = host.intake.current(c, actor, plan, rehearsal_id=receipt["rehearsal_id"])
        approved = client.post(f"/v1/plans/{SPEC.plan_id}/approvals",
            headers=headers | {"Idempotency-Key": "approve"}, json={"approval_kind": "change",
                "run_id": receipt["rehearsal_id"], "binding": current.binding, "expected_version": 1})
        assert approved.status_code == 200, approved.text
        assert client.post("/internal/v1/rehearsal-candidates/hold", headers=headers).status_code == 404
    assert [event[0] for event in flow["planner"].events] == ["create", "verify", "cleanup"]


def test_first_run_reaches_the_operator_rehearsal_service(flow, signing_key, monkeypatch):
    import bravo_platform_events.operator_access as operator_access

    seen = []
    real = operator_access.RehearsalService
    monkeypatch.setattr(operator_access, "RehearsalService",
                        lambda *a, **k: seen.append(k.get("first_run")) or real(*a, **k))
    def first_run(*_):
        return "proposal-run"
    ports = TrustedHostPorts(SimpleNamespace(submit=lambda *_: None, read=lambda *_: None),
        {"env-aws": "source", "env-onprem": "target"}, flow["service"].host.catalog,
        flow["planner"], lambda *_a, **_k: None, first_run=first_run)
    build_trusted_host(flow["store"], ports=ports, records=flow["records"], sender=flow["intake"].sender,
                       verifier=Identity(signing_key).verifier, token_supplier=lambda: flow["token"])
    assert seen == [first_run]


def test_missing_ports_are_rejected_before_host_creation():
    with pytest.raises(ValueError, match="trusted_host_ports_required"):
        TrustedHostPorts(object(), {}, object(), object(), None)


def test_dispatch_is_outside_request_bounded_and_never_retries():
    dispatcher = AttemptDispatcher()
    entered, release, finished = Event(), Event(), Event()
    calls = []
    request_thread = current_thread()
    def first():
        calls.append(current_thread())
        entered.set()
        assert release.wait(5)
        raise RuntimeError("fixture failure; must not retry")
    try:
        dispatcher(first)
        assert entered.wait(5)
        dispatcher(finished.set)
        with pytest.raises(EventError, match="rehearsal_dispatch_unavailable"):
            dispatcher(lambda: pytest.fail("third task must not run"))
        assert not finished.is_set()
        release.set()
        assert finished.wait(5)
    finally:
        release.set()
        dispatcher.close()
    assert len(calls) == 1 and calls[0] is not request_thread
    with pytest.raises(EventError, match="rehearsal_dispatch_unavailable"):
        dispatcher(lambda: None)
