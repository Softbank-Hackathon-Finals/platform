"""Actual T10 stores/locks and Ed25519 verification; identities/data are fixtures.

The security report is seeded as a fixture publisher would store it. These tests
do not claim actual T14 evidence, a deployed issuer, or cloud acceptance.
"""

import asyncio
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_gate.contract import T5Contract, canonical
from bravo_gate.engine import evaluate
from bravo_gate.models import GateContext, RunSnapshot, VerifiedAuthority
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey, Ed25519Authority
from bravo_platform_events.gate_records import GateRecords, _put
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_app import ORIGIN

from bravo_console import ConsoleApp
from bravo_console.observations import ObservationError
from bravo_console.reports import PlanControlReport
from bravo_console.t10_reports import T10ControlReports

SERVICES = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pg_store():
    path = SERVICES / "platform-events/tests/postgres_fixture.py"
    spec = importlib.util.spec_from_file_location("console_report_postgres_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pg = module.TemporaryPostgres()
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        pg = request.getfixturevalue("pg_store")
        return PostgresPlanStore(pg.conninfo(pg.database(), "fixture_runtime"))
    return PlanStore(tmp_path / "reports.db")


@pytest.fixture(params=["initial", "post_apply"])
def stored(request, store):
    phase = request.param
    principal = Principal("fixture-reader", "fixture-tenant")
    now = datetime.now(UTC) - timedelta(seconds=1)
    plan = PlanSpec("stored-plan", principal.tenant_id, "workload-fixture", "source-env",
                    "target-env", "a" * 64, "b" * 64, "demo-gate", "1", "apply-recheck", "1")
    store.register_plan(plan, run_id="fixture-registration", occurred_at=now.isoformat())
    f = SimpleNamespace(store=store, principal=principal, now=now, grant=True, identity=True,
                        calls=[], provenance_calls=0, hook=lambda: None, phase=phase,
                        application_id="fixture-application" if phase == "post_apply" else None)

    def authorize(c, actor, row, action):
        f.calls.append(action)
        if f.grant is not True:
            return f.grant
        return (actor == principal and row["plan_id"] == plan.plan_id
                and action in {"gate:record", "gate:bind", "gate:read"})

    f.records = GateRecords(store, authorize=authorize)
    contract = T5Contract()
    runs = {}
    for side, name in (("source", "source-run.json"), ("target", "target-run-after-fix.json")):
        doc = json.loads((SERVICES.parent / "docs/controls/examples" / name).read_text())
        doc.update(app=plan.workload_id, run_id="fixture-" + side,
                   started_at=now.isoformat(), finished_at=now.isoformat(),
                   environment=dict(id=getattr(plan, side + "_environment_id"), role=side,
                                    ref="fixture/" + side))
        for row in doc["results"]:
            if row["evidence"]:
                row["evidence"].update(collected_at=now.isoformat(),
                                       expires_at=(now + timedelta(minutes=10)).isoformat())
        runs[side] = RunSnapshot("fixture/" + side, canonical(doc))
        f.records.record_run(principal, plan.plan_id, side, runs[side])
    ctx = GateContext(principal.tenant_id, plan.workload_id, "fixture-gate", plan.plan_digest,
                      "apply-recheck" if phase == "post_apply" else "demo-gate", "1",
                      contract.catalog_digest, plan.source_environment_id, "fixture/source",
                      plan.target_environment_id, "fixture/target", runs["source"].ref,
                      runs["target"].ref, runs["source"].fingerprint, runs["target"].fingerprint)
    if phase == "post_apply":
        proof = dict(plan_id=plan.plan_id, plan_digest=plan.plan_digest,
                     target_environment_id=plan.target_environment_id, version=4,
                     receipt_id=f.application_id,
                     result=dict(application_id=f.application_id, job_passed=True,
                                 deployed_digest="sha256:" + "c" * 64,
                                 change=dict(image_digest="sha256:" + "c" * 64)))
        with store._transaction(write=True, plan_id=plan.plan_id) as c:
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                      (plan.plan_id, "workflow:4", "{}", json.dumps(proof)))
            c.execute("UPDATE plans SET state='GATE',version=5 WHERE plan_id=?", (plan.plan_id,))
    f.records.bind(principal, plan.plan_id, "fixture-context", ctx, expected_revision=0,
                   gate_phase=phase, application_id=f.application_id)
    doc = evaluate(contract, ctx, runs["source"].document, runs["target"].document, now=now)
    with store._transaction(write=True, plan_id=plan.plan_id) as c:
        _put(c, plan.plan_id, "security-report", ctx.gate_id, doc)
    report = RunSnapshot(ctx.gate_id, canonical(doc))
    signer = Ed25519PrivateKey.generate()
    f.key = AuthorityKey(signer.public_key().public_bytes_raw().hex(), (principal.tenant_id,),
                         ("gate-result",), (now - timedelta(minutes=1)).isoformat(),
                         (now + timedelta(minutes=10)).isoformat(), False)
    body = dict(issuer_id="fixture-issuer", key_id="fixture-key", purpose="gate-result",
                context_digest=ctx.fingerprint, subject_digest=report.fingerprint,
                issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())
    f.envelope = AuthorityEnvelope(**body, signature_hex=signer.sign(DOMAIN + canonical(body)).hex())

    def sign_report(document):
        payload = body | {"subject_digest": RunSnapshot(ctx.gate_id, canonical(document)).fingerprint}
        return AuthorityEnvelope(**payload, signature_hex=signer.sign(DOMAIN + canonical(payload)).hex())

    f.sign_report = sign_report
    authority = Ed25519Authority(envelope_provider=lambda *_: f.envelope,
                                key_provider=lambda *_: f.key, clock=lambda: f.now)

    def authenticate(actor, context, snapshot):
        f.provenance_calls += 1
        f.hook()
        if actor != principal.subject_id:
            return False
        return authority.verify(context, snapshot.fingerprint, "gate-result")

    def resolve(scope):
        if f.identity and Request(scope).cookies.get("fixture-session") == "test-only":
            return principal
        return None

    api = create_plan_app(store, resolve_principal=lambda req: resolve(req.scope))
    f.reader = T10ControlReports(f.records, resolve_principal=resolve,
                                authenticate_report=authenticate, clock=lambda: f.now)
    controls = PlanControlReport(api, load=f.reader.load, authenticate=f.reader.authenticate)
    f.app = ConsoleApp(api, public_origin=ORIGIN, controls=controls)
    f.context, f.report, f.plan, f.doc = ctx, report, plan, doc
    f.scope = dict(type="http", headers=[(b"cookie", b"fixture-session=test-only")])
    f.calls.clear()
    return f


def test_same_stored_report_reaches_console_without_writes(stored):
    f = stored
    before = f.store.snapshot(f.plan.plan_id)
    with f.store._transaction() as c:
        count = c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"]
    with TestClient(f.app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get(f"/v1/plans/{f.plan.plan_id}/controls")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["gate_id"] == f.context.gate_id and data["decision"] == f.doc["decision"]
        assert data["gate_phase"] == f.phase and data["application_id"] == f.application_id
        assert data["authorizes_cutover"] is False
        assert len(data["rows"]) == 3
    assert set(f.calls) == {"gate:read"} and f.provenance_calls == 3
    assert f.store.snapshot(f.plan.plan_id) == before
    with f.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"] == count


@pytest.mark.parametrize("fault", ["identity", "grant", "truthy-grant", "unsigned", "revoked", "expired", "tampered"])
def test_current_identity_grant_and_provenance_are_required(stored, fault):
    f = stored
    if fault == "identity":
        f.identity = False
    elif fault in {"grant", "truthy-grant"}:
        f.grant = False if fault == "grant" else 1
    elif fault == "unsigned":
        f.envelope = None
    elif fault == "revoked":
        f.key = replace(f.key, revoked=True)
    elif fault == "expired":
        f.now += timedelta(hours=1)
    else:
        # Simulate a compromised report transport, preserving the actual store
        # and its append-only privileges. The original issuer signed other bytes.
        current_view = f.records.view

        def view(*args):
            result = current_view(*args)
            read = result.report_provider

            def tampered(actor, ctx):
                original = read(actor, ctx)
                changed = original.document | {
                    "evaluated_at": (f.now - timedelta(seconds=1)).isoformat()
                }
                return RunSnapshot(original.ref, canonical(changed))

            result.report_provider = tampered
            return result

        f.records.view = view
    with TestClient(f.app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get(f"/v1/plans/{f.plan.plan_id}/controls")
        assert response.status_code == (403 if fault == "identity" else 503)
        assert "fixture-issuer" not in response.text and "signature" not in response.text
    if fault == "identity":
        assert not f.calls


def test_current_context_or_snapshot_change_invalidates_loaded_report(stored):
    f = stored
    snapshot = f.store.snapshot(f.plan.plan_id)
    candidate = asyncio.run(f.reader.load(f.scope, snapshot))
    wrong = replace(candidate, application_id="other-application")
    assert asyncio.run(f.reader.authenticate(f.scope, snapshot, wrong)) is False
    changed = replace(f.context, gate_id="new-gate")
    f.records.bind(f.principal, f.plan.plan_id, "new-context", changed, expected_revision=1,
                   gate_phase=f.phase, application_id=f.application_id)
    assert asyncio.run(f.reader.authenticate(f.scope, snapshot, candidate)) is False


def test_event_advance_requires_a_new_plan_snapshot(stored):
    f = stored
    before = f.store.snapshot(f.plan.plan_id)
    candidate = asyncio.run(f.reader.load(f.scope, before))
    f.store.append(plan_id=f.plan.plan_id, run_id="fixture-publisher",
                   event_type="gate.completed", producer_key="fixture-gate-event",
                   occurred_at=f.now.isoformat(), data=dict(gate_id=f.context.gate_id,
                       context_digest=f.context.fingerprint, decision=f.doc["decision"],
                       evidence_ref="fixture-report"))
    assert asyncio.run(f.reader.authenticate(f.scope, before, candidate)) is False
    current = f.store.snapshot(f.plan.plan_id)
    assert asyncio.run(f.reader.authenticate(f.scope, current, candidate)) is True


def test_authority_expiring_before_read_completion_is_rejected(stored):
    f = stored
    times = iter([f.now, f.now, f.now + timedelta(seconds=1)])
    f.reader.clock = lambda: next(times)
    f.reader.authenticate_report = lambda *_: VerifiedAuthority(
        f.context.fingerprint, f.report.fingerprint, "gate-result",
        f.now + timedelta(seconds=1))
    with pytest.raises(ObservationError, match="control_report_unavailable"):
        asyncio.run(f.reader.load(f.scope, f.store.snapshot(f.plan.plan_id)))


def test_grant_revoked_during_provenance_check_is_not_returned(stored):
    f = stored
    f.hook = lambda: setattr(f, "grant", False)
    with TestClient(f.app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get(f"/v1/plans/{f.plan.plan_id}/controls")
        assert response.status_code == 503


@pytest.mark.parametrize("fault", ["gate_id", "context_digest"])
def test_valid_signature_does_not_override_current_report_binding(stored, fault):
    f = stored
    changed = f.doc | {fault: "other-gate" if fault == "gate_id" else "e" * 64}
    f.envelope = f.sign_report(changed)
    current_view = f.records.view

    def view(*args):
        result = current_view(*args)
        result.report_provider = lambda *_: RunSnapshot(f.context.gate_id, canonical(changed))
        return result

    f.records.view = view
    with pytest.raises(ObservationError, match="control_report_unavailable"):
        asyncio.run(f.reader.load(f.scope, f.store.snapshot(f.plan.plan_id)))
    assert f.provenance_calls == 0


@pytest.mark.parametrize("fault", ["context", "report", "purpose", "expired"])
def test_scoped_authority_cannot_substitute_another_proof(stored, fault):
    f = stored
    values = dict(context_digest=f.context.fingerprint, subject_digest=f.report.fingerprint,
                  purpose="gate-result", valid_until=f.now + timedelta(minutes=1))
    if fault == "context":
        values["context_digest"] = "e" * 64
    elif fault == "report":
        values["subject_digest"] = "e" * 64
    elif fault == "purpose":
        values["purpose"] = "gate-run"
    else:
        values["valid_until"] = f.now
    f.reader.authenticate_report = lambda *_: VerifiedAuthority(**values)
    with TestClient(f.app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        assert client.get(f"/v1/plans/{f.plan.plan_id}/controls").status_code == 503


def test_wrong_tenant_and_snapshot_never_read_report(stored):
    f = stored
    snapshot = f.store.snapshot(f.plan.plan_id)
    f.reader.resolve_principal = lambda _: Principal(f.principal.subject_id, "different-tenant")
    with pytest.raises(ValueError, match="control_report_unavailable"):
        asyncio.run(f.reader.load(f.scope, snapshot))
    assert f.provenance_calls == 0
    f.reader.resolve_principal = lambda _: f.principal
    with pytest.raises(ValueError, match="control_report_unavailable"):
        asyncio.run(f.reader.load(f.scope, snapshot | {"version": snapshot["version"] + 1}))
    assert f.provenance_calls == 0


@pytest.mark.parametrize("error", [RuntimeError, ObservationError])
def test_host_diagnostic_text_is_never_an_http_error_code(stored, error):
    def broken(_):
        raise error("PRIVATE-RESOLVER-DIAGNOSTIC")

    stored.reader.resolve_principal = broken
    with TestClient(stored.app, base_url=ORIGIN,
                    cookies={"fixture-session": "test-only"}) as client:
        response = client.get(f"/v1/plans/{stored.plan.plan_id}/controls")
        assert response.status_code == 503
        assert "PRIVATE-RESOLVER-DIAGNOSTIC" not in response.text
        assert response.json()["detail"]["code"] == "control_report_unavailable"


def test_missing_provenance_and_async_identity_are_not_defaults(stored):
    async def async_identity(_):
        return stored.principal

    with pytest.raises(ValueError, match="explicit_t10_report_authority"):
        T10ControlReports(stored.records, resolve_principal=async_identity,
                          authenticate_report=lambda *_: True)
    with pytest.raises(ValueError, match="explicit_t10_report_authority"):
        T10ControlReports(stored.records, resolve_principal=lambda _: stored.principal,
                          authenticate_report=None)
