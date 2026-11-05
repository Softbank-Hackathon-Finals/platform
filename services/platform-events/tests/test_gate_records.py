"""Actual T10 transactions and T19 evaluator; probe data/auth are synthetic."""

import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot
from bravo_gate.service import GateService
from postgres_fixture import TemporaryPostgres

from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError


def _fixtures(name):
    path = Path(__file__).parents[2] / "control-gate/tests" / (name + ".py")
    spec = importlib.util.spec_from_file_location("gate_record_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_security = _fixtures("conftest")
contract, bundle, service = _security.contract, _security.bundle, _security.service
ready = _fixtures("test_readiness").ready


@pytest.fixture(scope="module")
def gate_pg():
    pg = TemporaryPostgres()
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture(params=["sqlite", "postgres"])
def records(request, tmp_path, ready):
    gate, state, data, doc, update = ready
    ctx = state["context"]
    if request.param == "postgres":
        pg = request.getfixturevalue("gate_pg")
        conninfo = pg.conninfo(pg.database(), "fixture_runtime")
        def reopen():
            return PostgresPlanStore(conninfo)
    else:
        def reopen():
            return PlanStore(tmp_path / "records.db")
    store = reopen()
    plan = PlanSpec("plan-gate-records", ctx.tenant_id, ctx.app, ctx.source_environment_id,
                    ctx.target_environment_id, ctx.plan_digest, "b" * 64)
    store.register_plan(plan, run_id="fixture", occurred_at=state["now"].isoformat())
    principal = Principal("operator", ctx.tenant_id)
    f = SimpleNamespace(store=store, reopen=reopen, state=state, data=data, doc=doc,
                        gate=gate, update=update, plan=plan, principal=principal,
                        allowed=True, calls=[], publish_hook=lambda: None)

    def authorize(c, p, row, action):
        f.calls.append((c, action))
        if f.allowed is not True:
            return f.allowed
        return p == principal and row["plan_id"] == plan.plan_id

    f.records = GateRecords(store, authorize=authorize)
    f.stage = lambda side: f.records.record_run(principal, plan.plan_id, side, state["runs"][side])
    f.bind = lambda: f.records.bind(principal, plan.plan_id, "context", state["context"], expected_revision=0)

    def factory(view):
        # Keep T19's current authentication callbacks. Stored bytes alone are
        # deliberately insufficient when the producer's trust is revoked.
        security = gate.security
        factory_gate = type(gate)(security=GateService(
            contract=security.contract, context_provider=view.context_provider,
            run_provider=view.run_provider, report_provider=view.report_provider,
            approval_provider=security.approval_provider,
            authenticate_context=security.authenticate_context,
            authenticate_run=security.authenticate_run,
            authenticate_report=security.authenticate_report,
            authenticate_approval=security.authenticate_approval,
            authorize=security.authorize, clock=security.clock),
            binding_provider=gate.binding_provider, snapshot_provider=gate.snapshot_provider,
            authenticate_binding=gate.authenticate_binding,
            authenticate_snapshot=gate.authenticate_snapshot)
        f.publish_hook()
        return factory_gate

    f.factory = factory
    f.evaluate = lambda: f.records.evaluate(principal, plan.plan_id, service_factory=factory)
    return f


def _view(f):
    return f.store._transaction(write=True, plan_id=f.plan.plan_id)


def _load(f, c):
    plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
    return f.records.view(c, f.principal, plan)


def test_source_only_stages_but_missing_target_cannot_bind(records):
    f = records
    receipt = f.stage("source")
    assert receipt == f.stage("source")
    with _view(f) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        assert f.records.read_run(c, f.principal, plan, receipt["ref"]) == f.state["runs"]["source"]
    with pytest.raises(EventError, match="gate_run_unavailable"):
        f.bind()
    with pytest.raises(EventError, match="gate_context_unavailable"):
        f.evaluate()
    assert f.store.snapshot(f.plan.plan_id)["state"] == "COLLECT"


def test_persist_reopen_evaluate_and_consume_same_connection(records):
    f = records
    f.stage("source"), f.stage("target"), f.bind()
    report = f.evaluate()
    assert report["decision"] == "PASS"
    assert f.evaluate() == report
    f.records.store = f.store = f.reopen()
    with _view(f) as c:
        view = _load(f, c)
        session = view.session(f.factory(view), f.data["binding"])
        proof = session.service.require_prerequisites(f.principal.subject_id,
            session.context_id, session.report.raw)
        assert not proof.authorizes_cutover
        assert session.context == f.state["context"]
        assert all(connection is c for connection, _ in f.calls[-4:])
    assert f.store.snapshot(f.plan.plan_id)["state"] == "COLLECT"


@pytest.mark.parametrize("fault", ["tenant", "actor", "revoked", "truthy"])
def test_current_permission_and_tenant_precede_reads_and_writes(records, fault):
    f = records
    f.stage("source"), f.stage("target"), f.bind()
    if fault == "tenant":
        f.principal = replace(f.principal, tenant_id="other-tenant")
    elif fault == "actor":
        f.principal = replace(f.principal, subject_id="other-operator")
    else:
        f.allowed = False if fault == "revoked" else "allowed"
    with pytest.raises(EventError, match="forbidden"):
        f.records.record_run(f.principal, f.plan.plan_id, "source", f.state["runs"]["source"])
    with _view(f) as c, pytest.raises(EventError, match="forbidden"):
        _load(f, c)


def test_same_reference_cannot_change_original(records):
    f = records
    f.stage("source")
    original = f.state["runs"]["source"]
    doc = original.document
    doc["results"][0].update(verdict="FAIL", observed="allow")
    with pytest.raises(EventError, match="gate_record_conflict"):
        f.records.record_run(f.principal, f.plan.plan_id, "source", RunSnapshot(original.ref, canonical(doc)))
    f.stage("target"), f.bind()
    assert f.evaluate()["decision"] == "PASS"


@pytest.mark.parametrize("fault", ["source_as_target", "app", "environment", "context_plan"])
def test_scope_mismatch_never_binds(records, fault):
    f = records
    if fault == "context_plan":
        f.stage("source"), f.stage("target")
        with pytest.raises(EventError, match="gate_context_scope_mismatch"):
            f.records.bind(f.principal, f.plan.plan_id, "context",
                           replace(f.state["context"], plan_digest="c" * 64), expected_revision=0)
        return
    snapshot = f.state["runs"]["source"]
    doc = snapshot.document
    if fault == "app":
        doc["app"] = "other-app"
    elif fault == "environment":
        doc["environment"]["id"] = "other-environment"
    with pytest.raises(EventError, match="gate_run_scope_mismatch"):
        f.records.record_run(f.principal, f.plan.plan_id,
            "target" if fault == "source_as_target" else "source", RunSnapshot(snapshot.ref, canonical(doc)))


def test_old_binding_retry_does_not_restore_old_context(records):
    f = records
    f.stage("source"), f.stage("target"), f.bind()
    next_context = replace(f.state["context"], gate_id="next-gate")
    f.records.bind(f.principal, f.plan.plan_id, "context-2", next_context, expected_revision=1)
    assert f.bind()["revision"] == 1
    with _view(f) as c:
        assert _load(f, c).context_id == "context-2"
    with pytest.raises(EventError, match="version_conflict"):
        f.records.bind(f.principal, f.plan.plan_id, "context-3", next_context, expected_revision=1)


@pytest.mark.parametrize("fault", ["producer_revoked", "schema_mismatch"])
def test_persistence_never_bypasses_t19_checks(records, fault):
    f = records
    f.stage("source"), f.stage("target"), f.bind()
    if fault == "producer_revoked":
        f.state["trusted"] = False
        with pytest.raises(GateError, match="AUTHORITY_DENIED"):
            f.evaluate()
    else:
        f.doc["target"]["tables"][0]["schema_hash"] = "c" * 64
        f.update(f.doc)
        assert f.evaluate()["decision"] == "BLOCK"
    with _view(f) as c:
        view = _load(f, c)
        if fault == "producer_revoked":
            with pytest.raises(EventError, match="gate_report_unavailable"):
                view.session(f.factory(view), f.data["binding"])
        else:
            session = view.session(f.factory(view), f.data["binding"])
            with pytest.raises(GateError, match="COMBINED_GATE_BLOCKED"):
                session.service.require_prerequisites(f.principal.subject_id, session.context_id, session.report.raw)


def test_revocation_during_evaluation_rolls_back_report(records):
    f = records
    f.stage("source"), f.stage("target"), f.bind()
    f.publish_hook = lambda: setattr(f, "allowed", False)
    with pytest.raises(GateError):
        f.evaluate()
    with _view(f) as c:
        rows = c.execute("SELECT command_key FROM plan_commands WHERE plan_id=?", (f.plan.plan_id,)).fetchall()
        assert not any("report" in row["command_key"] for row in rows)


def test_durable_operator_grant_revocation_is_read_inside_plan_transaction(records):
    f = records
    f.stage("source"), f.stage("target"), f.bind(), f.evaluate()
    now = f.state["now"]
    principal = OperatorPrincipal("reader", f.plan.tenant_id, int(now.timestamp()),
                                  int(now.timestamp()) + 300, int(now.timestamp()), "session-fixture")
    access = OperatorAccess(f.store, clock=lambda: now.isoformat())

    def grant(permissions, revision):
        access.set_permissions(f.plan.plan_id, principal, permissions=permissions,
            valid_after=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(),
            expected_revision=revision, administration_ref="fixture-admin",
            command_key=f"grant-{revision}")

    def authorize(c, actor, plan, action):
        if action != "gate:read":
            return False
        access.require(c, actor, plan, "read")
        return True

    grant(["read"], 0)
    f.principal = principal
    f.records = GateRecords(f.reopen(), authorize=authorize)
    with _view(f) as c:
        assert _load(f, c).run_provider(principal.subject_id, f.state["context"], "source") == f.state["runs"]["source"]
    # Read-only users cannot stage evidence even while their read grant is live.
    with pytest.raises(EventError, match="forbidden"):
        f.records.record_run(principal, f.plan.plan_id, "source", f.state["runs"]["source"])
    grant([], 1)
    with _view(f) as c, pytest.raises(EventError, match="forbidden"):
        _load(f, c)
