"""Real T10 SQLite/PG and Ed25519; synthetic registered identities/probe data."""

import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot, VerifiedAuthority, permission_subject
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey, Ed25519Authority
from bravo_gate.stored import StoredGateServices


@pytest.fixture(scope="module")
def stored_postgres():
    path = Path(__file__).parents[2] / "platform-events/tests/postgres_fixture.py"
    spec = importlib.util.spec_from_file_location("stored_services_pg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cluster = module.TemporaryPostgres()
    try:
        yield cluster
    finally:
        cluster.close()


@pytest.fixture(params=["sqlite", "postgres"])
def stored(request, tmp_path, ready):  # noqa: F811 - imported shared fixture
    gate, state, data, doc, update = ready
    ctx = state["context"]
    if request.param == "sqlite":
        store = PlanStore(tmp_path / "stored.db")
    else:
        pg = request.getfixturevalue("stored_postgres")
        store = PostgresPlanStore(pg.conninfo(pg.database(), "fixture_runtime"))
    principal = Principal("operator", ctx.tenant_id)
    plan = PlanSpec("stored-services", ctx.tenant_id, ctx.app, ctx.source_environment_id,
                    ctx.target_environment_id, ctx.plan_digest, "b" * 64,
                    profile_id=ctx.profile_id, profile_version=ctx.profile_version)
    store.register_plan(plan, run_id="synthetic", occurred_at=state["now"].isoformat())
    f = SimpleNamespace(state=state, data=data, doc=doc, update=update, store=store,
                        principal=principal, plan=plan, allowed=True, keys={}, envelopes={},
                        private_keys={}, permission_until=state["now"] + timedelta(seconds=20),
                        connections=[], calls=[], hook=lambda: None)

    def grant(c, p, registered, action):
        f.connections.append(c)
        f.calls.append(action)
        return (f.allowed is True and p == principal
                and registered["plan_id"] == plan.plan_id)

    f.records = GateRecords(store, authorize=grant)

    def sign(purpose, subject):
        now = state["now"]
        if purpose not in f.private_keys:
            key = Ed25519PrivateKey.generate()
            f.private_keys[purpose] = key
            f.keys[purpose] = AuthorityKey(key.public_key().public_bytes_raw().hex(),
                (ctx.tenant_id,), (purpose,), (now - timedelta(seconds=1)).isoformat(),
                (now + timedelta(minutes=2)).isoformat(), False)
        value = AuthorityEnvelope(purpose, "key-one", purpose, state["context"].fingerprint,
                                  subject, now.isoformat(),
                                  (now + timedelta(seconds=25)).isoformat(), "0" * 128)
        f.envelopes[purpose, subject] = replace(value, signature_hex=f.private_keys[purpose].sign(
            DOMAIN + canonical(value.payload)).hex())
    f.sign = sign

    def authority(view):
        def envelope(context, subject, purpose):
            assert view.c is f.connections[-1]
            f.hook()
            return f.envelopes.get((purpose, subject))
        return Ed25519Authority(envelope_provider=envelope,
                                key_provider=lambda issuer, key_id: f.keys.get(issuer),
                                clock=lambda: state["now"])

    def authorize(view, actor, context, action):
        assert view.c is f.connections[-1]
        return VerifiedAuthority(context.fingerprint,
                                 permission_subject(actor, context.fingerprint, action),
                                 "permission:" + action, f.permission_until)

    f.factory = StoredGateServices(authority_provider=authority, authorize=authorize,
        approval_provider=lambda *_: None,
        binding_provider=lambda *_: data["binding"],
        snapshot_provider=lambda *_: data["snapshot"], clock=lambda: state["now"])

    def prepare():
        for side in ("source", "target"):
            f.records.record_run(principal, plan.plan_id, side, state["runs"][side])
            sign("run-" + side, state["runs"][side].fingerprint)
        f.records.bind(principal, plan.plan_id, "context", state["context"], expected_revision=0)
        sign("context", state["context"].fingerprint)
        sign("readiness-binding", data["binding"].fingerprint)
        sign("readiness-snapshot", data["snapshot"].fingerprint)
    f.prepare = prepare
    f.evaluate = lambda: f.records.evaluate(principal, plan.plan_id, service_factory=f.factory)
    return f


def test_signed_stored_evaluation_and_current_consumption(stored):
    f = stored
    f.prepare()
    before = f.store.snapshot(f.plan.plan_id)
    report = f.evaluate()
    assert report["decision"] == "PASS"
    assert report == f.evaluate()
    assert f.store.snapshot(f.plan.plan_id) == before
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.principal, plan)
        security_report = view.report_provider(f.principal.subject_id, view.context)
        f.sign("gate-result", security_report.fingerprint)
        service = f.factory(view)
        proof = service.require_prerequisites(f.principal.subject_id, view.context_id,
                                              canonical(report))
        assert not proof.authorizes_cutover
        assert proof.context_digest == view.context.fingerprint
        f.keys["run-target"] = replace(f.keys["run-target"], revoked=True)
        with pytest.raises(GateError):
            service.require_prerequisites(f.principal.subject_id, view.context_id, canonical(report))
    assert "gate:evaluate" in f.calls


@pytest.mark.parametrize("purpose", ["context", "run-source", "run-target",
                                    "readiness-binding", "readiness-snapshot"])
def test_stored_bytes_without_current_registered_envelope_are_rejected(stored, purpose):
    f = stored
    f.prepare()
    f.envelopes = {k: v for k, v in f.envelopes.items() if k[0] != purpose}
    with pytest.raises(GateError, match="SIGNATURE_BINDING_MISMATCH"):
        f.evaluate()


@pytest.mark.parametrize("fault", ["expired", "tampered", "revoked", "wrong-purpose", "wrong-context"])
def test_real_signature_failure_blocks_stored_gate(stored, fault):
    f = stored
    f.prepare()
    subject = f.state["runs"]["target"].fingerprint
    index = ("run-target", subject)
    if fault == "expired":
        f.state["now"] += timedelta(seconds=26)
    elif fault == "tampered":
        f.envelopes[index] = replace(f.envelopes[index], signature_hex="0" * 128)
    elif fault == "revoked":
        f.keys["run-target"] = replace(f.keys["run-target"], revoked=True)
    elif fault == "wrong-purpose":
        f.keys["run-target"] = replace(f.keys["run-target"], purposes=("run-source",))
    else:
        f.envelopes[index] = replace(f.envelopes[index], context_digest="f" * 64)
    with pytest.raises(GateError):
        f.evaluate()


@pytest.mark.parametrize("fault", ["grant", "expiry", "provider-secret"])
def test_grants_expiry_and_provider_failures_do_not_leave_a_report(stored, fault):
    f = stored
    f.prepare()
    if fault == "grant":
        f.hook = lambda: setattr(f, "allowed", False)
    elif fault == "expiry":
        f.permission_until = f.state["now"]
    else:
        def broken(*_):
            raise RuntimeError("password=private-test-marker")
        f.factory.snapshot_provider = broken
    with pytest.raises((GateError, EventError)) as error:
        f.evaluate()
    assert "private-test-marker" not in str(error.value)
    with f.store._transaction() as c:
        assert not c.execute("SELECT command_key FROM plan_commands WHERE command_key LIKE ?",
                             ("gate-security-report-%",)).fetchall()


@pytest.mark.parametrize("fault", ["open-bucket", "schema", "unknown"])
def test_authenticated_failure_still_blocks(stored, fault):
    f = stored
    if fault == "schema":
        f.doc["target"]["tables"][0]["schema_hash"] = "c" * 64
        f.update(f.doc)
    else:
        run = f.state["runs"]["target"]
        document = run.document
        document["results"][0].update(verdict="FAIL", observed="allow")
        if fault == "unknown":
            document["results"][0].update(verdict="UNKNOWN", unknown_reason="NOT_RUN",
                                          observed=None, evidence=None, positive_control="not_applicable")
        f.state["runs"]["target"] = RunSnapshot(run.ref, canonical(document))
        f.state["context"] = replace(f.state["context"],
                                     target_digest=f.state["runs"]["target"].fingerprint)
        f.update(f.doc)
    f.prepare()
    assert f.evaluate()["decision"] == "BLOCK"


def test_factory_has_no_default_authority_or_unregistered_view(stored):
    f = stored
    with pytest.raises(GateError, match="SERVER_RECORD_VIEW_REQUIRED"):
        f.factory(SimpleNamespace())
    f.prepare()
    f.factory.authority_provider = lambda *_: True
    with pytest.raises(GateError, match="REGISTERED_SIGNATURE_AUTHORITY_REQUIRED"):
        f.evaluate()


@pytest.mark.parametrize("provider", ["authority_provider", "authorize", "approval_provider",
                                     "binding_provider", "snapshot_provider", "clock"])
def test_missing_host_provider_is_not_silently_accepted(provider):
    values = {name: lambda *_: None for name in (
        "authority_provider", "authorize", "approval_provider", "binding_provider",
        "snapshot_provider", "clock")}
    values[provider] = None
    with pytest.raises(GateError, match="EXPLICIT_SERVER_AUTHORITY_REQUIRED"):
        StoredGateServices(**values)


def test_platform_reevaluation_is_its_own_provenance_only_inside_the_shift_check(stored):
    """sangmu1126 #565 ②: with the real Ed25519 authority, a fresh report has no signed gate-result.
    Inside served_reevaluation (same transaction, SHIFT/cutover only) the server record is its
    provenance; the same document consumed outside it is still refused."""
    from bravo_platform_events.gate_records import served_reevaluation

    f = stored
    f.prepare()
    f.evaluate()
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.principal, plan)
        service = f.factory(view)
        binding = service.binding_provider(f.principal.subject_id, view.context)
        session = view.session(service, binding)
        with served_reevaluation(c, plan["plan_id"], session, f.principal) as fresh:
            proof = service.require_prerequisites(f.principal.subject_id, view.context_id, canonical(fresh))
            assert proof.context_digest == view.context.fingerprint
        with pytest.raises(GateError):
            service.require_prerequisites(f.principal.subject_id, view.context_id, canonical(fresh))
