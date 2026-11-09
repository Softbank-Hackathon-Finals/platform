"""Real SigV4 verification and plan transactions; no AWS/cluster/model calls."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_registration_sso as sso
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_change_intake import PLAN
from test_registration_sso import proof as proof
from test_rehearsal import Scenario
from test_rehearsal import signing_key as signing_key
from test_t10_recorded_flow import SPEC
from test_t10_recorded_flow import flow as flow

from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.registration import Registry
from bravo_platform_events.rehearsal import RehearsalService
from bravo_platform_events.rehearsal_server import RehearsalPorts, build_host
from bravo_platform_events.rehearsal_sso import (
    AUDIENCE,
    RehearsalGrantFile,
    SsoRehearsalPrincipal,
    SsoRehearsalVerifier,
    create_rehearsal_app,
    make_rehearsal_token,
)
from bravo_platform_events.store import EventError


@pytest.fixture
def identity(proof, monkeypatch):
    registration_token = proof.token
    monkeypatch.setitem(sso.CONFIG, "audience", AUDIENCE)
    # Use the same actual SigV4 signer and synthetic STS signature verifier.
    token = make_rehearsal_token(profile_name="test-explicit", region=sso.CONFIG["region"])
    verifier = SsoRehearsalVerifier(sso.verifier(proof))
    actor = verifier(Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}))
    return SimpleNamespace(proof=proof, token=token, registration_token=registration_token,
                           verifier=verifier, actor=actor)


def grant_file(tmp_path, actor, plan_id):
    path = tmp_path / "grants.json"
    grant = dict(subject_id=actor.subject_id, tenant_id=actor.tenant_id, plan_id=plan_id,
        valid_after=datetime.fromtimestamp(sso.NOW.timestamp()-1, UTC).isoformat(),
        expires_at=(sso.NOW+timedelta(hours=1)).isoformat(), administration_ref="explicit-user-rehearsal")
    path.write_text(json.dumps(dict(version=1, grants=[grant])))
    return path, grant


@pytest.fixture
def api(backend, tmp_path, signing_key, identity):
    scenario = Scenario(backend, signing_key)
    # Match the registered fixture tenant without changing the proof's identity.
    identity.verifier.identity.tenant_id = PLAN.tenant_id
    actor = replace(identity.actor, tenant_id=identity.verifier.identity.tenant_id)
    path, grant = grant_file(tmp_path, actor, "plan-fixture")
    access = RehearsalGrantFile(path, clock=lambda: identity.proof.clock[0])
    service = RehearsalService(backend.store, host=scenario.worker, check_start=access.require)
    app = create_rehearsal_app(service, verifier=identity.verifier, access=access)
    return SimpleNamespace(scenario=scenario, identity=identity, actor=actor, path=path, grant=grant,
                           access=access, service=service, app=app)


def headers(api):
    return {"Authorization": "Bearer " + api.identity.token, "Idempotency-Key": "start"}


START = "/v1/plans/plan-fixture/rehearsals"
BODY = {"task_id": "bucket", "expected_version": 1, "attempt": 1}


def test_sso_start_is_once_and_revocation_precedes_cached_receipt(api):
    with TestClient(api.app, base_url="https://host.invalid") as client:
        first = client.post(START, json=BODY, headers=headers(api))
        assert first.status_code == 202, first.text
        assert client.post(START, json=BODY, headers=headers(api)).json() == first.json()
        assert len(api.scenario.worker.effects) == 1
        api.path.write_text('{"version":1,"grants":[]}')
        assert client.post(START, json=BODY, headers=headers(api)).status_code == 403
        assert client.get("/v1/plans/plan-fixture", headers=headers(api)).status_code == 403
    assert len(api.scenario.worker.effects) == 1


def test_lost_submission_ack_does_not_resubmit(api):
    api.scenario.worker.lose_ack = True
    with TestClient(api.app, base_url="https://host.invalid") as client:
        assert client.post(START, json=BODY, headers=headers(api)).status_code == 503
        assert client.post(START, json=BODY, headers=headers(api)).status_code == 202
    assert len(api.scenario.worker.effects) == 1


@pytest.mark.parametrize("kind", ["registration-proof", "expired-proof", "missing-grant", "wrong-plan", "expired-grant", "duplicate-grant", "bad-grant", "other-tenant"])
def test_invalid_authority_has_no_admission_or_remote_effect(api, kind):
    h = headers(api)
    if kind == "registration-proof":
        h["Authorization"] = "Bearer " + api.identity.registration_token
    elif kind == "expired-proof":
        api.identity.proof.clock[0] += 60
    else:
        grant = dict(api.grant)
        if kind == "wrong-plan":
            grant["plan_id"] = "other-plan"
        if kind == "expired-grant":
            grant["expires_at"] = sso.NOW.isoformat()
        if kind == "other-tenant":
            grant["tenant_id"] = "other-tenant"
        if kind == "bad-grant":
            grant["permissions"] = ["approve_change"]
        api.path.write_text(json.dumps(dict(version=1, grants=[] if kind == "missing-grant"
            else [grant, grant] if kind == "duplicate-grant" else [grant])))
    with TestClient(api.app, base_url="https://host.invalid") as client:
        assert client.post(START, json=BODY, headers=h).status_code == 403
    assert api.scenario.worker.effects == []


def test_no_approval_cutover_hold_registration_or_grant_routes(api):
    with TestClient(api.app, base_url="https://host.invalid") as client:
        for path in ("/v1/plans/plan-fixture/approvals", "/v1/plans/plan-fixture/execute", "/v1/environments",
                     "/internal/v1/rehearsal-candidates", "/internal/v1/rehearsal-candidates/hold", "/grants"):
            assert client.post(path, json={}, headers=headers(api)).status_code == 404
        assert client.post(START, json=BODY | {"attempt": 2}, headers=headers(api)).status_code == 422
    with TestClient(api.app, base_url="http://host.invalid") as client:
        assert client.post(START, json=BODY, headers=headers(api)).status_code == 403
    assert api.scenario.worker.effects == []


def test_new_identity_cannot_be_used_as_registration_or_operator_approval(api):
    assert isinstance(api.actor, SsoRehearsalPrincipal) and not isinstance(api.actor, OperatorPrincipal)
    registry = Registry(api.scenario.backend.store, environments={}, releases=[], can_register=lambda _: True,
                        clock=lambda: sso.NOW)
    with pytest.raises(EventError, match="forbidden"):
        registry.authorize(api.actor)
    access = OperatorAccess(api.scenario.backend.store, clock=lambda: sso.NOW.isoformat())
    with pytest.raises(EventError, match="forbidden"):
        access.set_permissions("plan-fixture", api.actor, permissions=["read", "approve_change"],
            valid_after=sso.NOW.isoformat(), expires_at=(sso.NOW+timedelta(hours=1)).isoformat(),
            expected_revision=0, administration_ref="not-approval-authority", command_key="grant")


def test_server_failure_returns_fixed_error_without_credentials(api, monkeypatch):
    def unavailable(_request):
        raise RuntimeError("PRIVATE_AWS_DIAGNOSTIC")
    monkeypatch.setattr(api.identity.verifier.identity, "verify", unavailable)
    with TestClient(api.app, base_url="https://host.invalid") as client:
        result = client.post(START, json=BODY, headers=headers(api))
        assert result.status_code == 503
        assert "PRIVATE_AWS_DIAGNOSTIC" not in result.text and api.identity.token not in result.text


def test_proof_expiry_is_checked_again_inside_plan_lock(api, monkeypatch):
    start = api.service.start

    def delayed(*args, **kwargs):
        api.identity.proof.clock[0] += 60
        return start(*args, **kwargs)

    monkeypatch.setattr(api.service, "start", delayed)
    with TestClient(api.app, base_url="https://host.invalid") as client:
        assert client.post(START, json=BODY, headers=headers(api)).status_code == 403
    assert api.scenario.worker.effects == []


def test_database_diagnostics_are_unavailable_not_public(api, monkeypatch):
    import sqlite3

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("PRIVATE_DATABASE_DIAGNOSTIC")

    monkeypatch.setattr(api.service, "start", unavailable)
    with TestClient(api.app, base_url="https://host.invalid") as client:
        result = client.post(START, json=BODY, headers=headers(api))
        assert result.status_code == 503 and "PRIVATE_DATABASE_DIAGNOSTIC" not in result.text


def test_registration_audience_cannot_construct_rehearsal_verifier(proof):
    with pytest.raises(ValueError, match="rehearsal_sso_identity_required"):
        SsoRehearsalVerifier(sso.verifier(proof))


def test_real_t10_t15_signed_intake_stops_before_human_approval(flow, identity, tmp_path):
    identity.verifier.identity.tenant_id = SPEC.tenant_id
    actor = replace(identity.actor, tenant_id=SPEC.tenant_id)
    path, _ = grant_file(tmp_path, actor, SPEC.plan_id)
    host = build_host(flow["store"], ports=RehearsalPorts(flow["service"].host.catalog, flow["planner"]),
        records=flow["records"], sender=flow["intake"].sender, verifier=identity.verifier,
        grants=RehearsalGrantFile(path, clock=lambda: identity.proof.clock[0]), token_supplier=lambda: flow["token"])
    assert host.evidence.store is flow["records"]
    assert flow["planner"].events == []
    h = {"Authorization": "Bearer " + identity.token, "Idempotency-Key": "start"}
    with TestClient(host.app, base_url="https://host.invalid") as client:
        first = client.post(START, json=BODY, headers=h)
        assert first.status_code == 202, first.text
        run = first.json()["rehearsal_id"]
        for _ in range(150):
            observed = client.post(f"/v1/plans/{SPEC.plan_id}/rehearsals/{run}/attempts/1/reconcile", headers=h)
            if observed.status_code == 200 and observed.json().get("status") == "PASS":
                break
            assert observed.status_code in (200, 503), observed.text
            time.sleep(0.01)
        assert observed.status_code == 200 and observed.json()["status"] == "PASS", observed.text
        assert client.post(f"/v1/plans/{SPEC.plan_id}/approvals", json={}, headers=h).status_code == 404
        with flow["store"]._transaction() as c:
            plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
            assert host.intake.current(c, actor, plan, rehearsal_id=run).binding
    assert [event[0] for event in flow["planner"].events] == ["create", "verify", "cleanup"]
