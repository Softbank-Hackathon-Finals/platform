"""Real operator journal and T19 evaluation; all identities/evidence are synthetic."""

import importlib.util
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot
from bravo_gate.t10 import PlanGateSession
from bravo_observability.sli import Record, build_window
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError
from test_control import BINDING, Provider
from test_host import PROFILE

from bravo_edge_control.authority import ShiftAuthority
from bravo_edge_control.provider import Cloudflare, EdgeError
from bravo_edge_control.run import load_runtime
from bravo_edge_control.runtime import RuntimePorts


def fixture_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE_TESTS = Path(__file__).resolve().parents[2] / "control-gate/tests"
gate_fixtures = fixture_module("edge_gate_fixtures", GATE_TESTS / "conftest.py")
readiness_fixtures = fixture_module("edge_readiness_fixtures", GATE_TESTS / "test_readiness.py")
contract, bundle, service = gate_fixtures.contract, gate_fixtures.bundle, gate_fixtures.service
ready = readiness_fixtures.ready


@pytest.fixture(scope="module")
def cluster():
    module = fixture_module("edge_t10_postgres_fixture", Path(__file__).resolve().parents[2]
                            / "platform-events/tests/postgres_fixture.py")
    pg = module.TemporaryPostgres()
    try:
        with pg.connect("platform_template", "fixture_migration") as c:
            c.execute((Path(__file__).resolve().parents[2]
                       / "platform-events/sql/0002_sli_windows.sql").read_text())
        yield pg
    finally:
        pg.close()


@pytest.fixture(params=["sqlite", "postgres"])
def bound(ready, tmp_path, request):
    gate, state, data, _, _ = ready
    ctx = state["context"]
    if request.param == "postgres":
        pg = request.getfixturevalue("cluster")
        store = PostgresPlanStore(pg.conninfo(pg.database(), "fixture_runtime"))
    else:
        store = PlanStore(tmp_path / "plans.db")
    plan = PlanSpec("plan-runtime", ctx.tenant_id, ctx.app, ctx.source_environment_id,
        ctx.target_environment_id, ctx.plan_digest, "b" * 64, ctx.profile_id, ctx.profile_version)
    store.register_plan(plan, run_id="fixture-registration", occurred_at=state["now"].isoformat())
    report = RunSnapshot(ctx.gate_id, canonical(gate.evaluate("actor", "context")))
    proof = dict(plan_id=plan.plan_id, version=6, plan_digest=plan.plan_digest,
        target_environment_id=ctx.target_environment_id, receipt_id="fixture-shift-approval",
        result=dict(approval_id="approved-fixture", approved=True,
            expires_at=(state["now"] + timedelta(minutes=4)).isoformat(),
            gate=dict(verdict="PASS", gate_id=ctx.gate_id, context_digest=ctx.fingerprint)))
    with store._transaction(write=True, plan_id=plan.plan_id) as c:
        c.execute("UPDATE plans SET state='SHIFT', version=7 WHERE plan_id=?", (plan.plan_id,))
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (plan.plan_id, "workflow:6", "{}", json.dumps(proof)))
    at = int(state["now"].timestamp())
    actor = OperatorPrincipal("actor", ctx.tenant_id, at, at + 300, at, "fixture-session")

    def session(c, principal, p):
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (p["plan_id"],)).fetchone()["state"] == "SHIFT"
        return PlanGateSession(gate, plan.plan_id, "context", state["context"], report, data["binding"])

    authority = ShiftAuthority(store, load_session=session, clock=lambda: state["now"].isoformat())
    grant = authority.access.set_permissions(plan.plan_id, actor,
        permissions=["read", "approve_shift", "execute_shift"], valid_after=state["now"].isoformat(),
        expires_at=(state["now"] + timedelta(minutes=4)).isoformat(), expected_revision=0,
        administration_ref="fixture-human-decision", command_key="fixture-grant")

    def current():
        with store._transaction(write=True, plan_id=plan.plan_id) as c:
            p = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan.plan_id,)).fetchone())
            return authority(c, actor, p)

    def register(**changes):
        return authority.register(actor, plan.plan_id,
            **(dict(run_id="run-registered", profile=PROFILE, binding=BINDING) | changes))

    return SimpleNamespace(authority=authority, store=store, plan=plan, actor=actor, state=state,
                           data=data, current=current, register=register, grant=grant)


def test_register_binds_existing_workflow_and_consumes_real_t19(bound):
    f = bound
    with pytest.raises(EdgeError, match="registration_unavailable"):
        f.current()
    saved = f.register()
    assert f.register() == saved
    current = f.current()
    assert current.approval_id == "approved-fixture"
    assert current.run_id == "run-registered"
    assert f.store.snapshot(f.plan.plan_id)["state"] == "SHIFT"
    with pytest.raises(EdgeError, match="record_conflict"):
        f.register(run_id="replacement-run")


@pytest.mark.parametrize("fault", ["gate_permission", "gate_authentication", "data_authentication", "gate_expired", "operator_revoked", "operator_regranted", "approval_revoked", "plan_state"])
def test_current_authority_rejects_revocation_and_expiry(bound, fault):
    f = bound
    f.register()
    if fault == "gate_permission":
        f.state["allowed"] = False
    elif fault == "gate_authentication":
        f.state["trusted"] = False
    elif fault == "data_authentication":
        f.data["trusted"] = False
    elif fault == "gate_expired":
        f.state["now"] += timedelta(seconds=61)
    elif fault in {"operator_revoked", "operator_regranted"}:
        f.authority.access.set_permissions(f.plan.plan_id, f.actor, permissions=[],
            valid_after=f.grant["valid_after"], expires_at=f.grant["expires_at"], expected_revision=1,
            administration_ref="fixture-revoke", command_key="fixture-revoke")
        if fault == "operator_regranted":
            f.authority.access.set_permissions(f.plan.plan_id, f.actor,
                permissions=f.grant["permissions"], valid_after=f.grant["valid_after"],
                expires_at=f.grant["expires_at"], expected_revision=2,
                administration_ref="fixture-regrant", command_key="fixture-regrant")
    elif fault == "approval_revoked":
        f.authority.revoke(f.actor, f.plan.plan_id, approval_id="approved-fixture")
        f.register()  # Exact registration retry cannot clear revocation.
    else:
        with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
            c.execute("UPDATE plans SET state='PROPOSE' WHERE plan_id=?", (f.plan.plan_id,))
    with pytest.raises((EdgeError, EventError, GateError)):
        f.current()


def test_installed_runtime_composes_real_grants_gate_windows_and_abort(bound, tmp_path):
    f = bound
    upstream = Provider()
    provider = Cloudflare(BINDING, lambda: "fixture-token", transport=httpx.MockTransport(upstream))
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(dict(version=1, plan_id=f.plan.plan_id,
        run_id="run-registered", profile=PROFILE, lb_binding=asdict(BINDING),
        journal=str(tmp_path / "edge.db"))))
    runtime, authority, _ = load_runtime(f.store, config,
        lambda _: RuntimePorts(lambda: f.actor, f.authority.load_session, provider),
        clock=lambda: f.state["now"].isoformat())
    authority.register(f.actor, f.plan.plan_id, run_id="run-registered", profile=PROFILE, binding=BINDING)

    def receive(value):
        if "receipt" not in value:
            return
        start = f.state["now"]
        f.state["now"] += timedelta(seconds=10)
        window = build_window([Record("source", 200, 100)] * 1800
                              + [Record("target", 500, 100)] * 200,
            plan_id=f.plan.plan_id, start=start.isoformat(), end=f.state["now"].isoformat(),
            pools={"source": "source", "target": "target"})
        with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
            c.execute("INSERT INTO sli_windows VALUES (?,?,?,?)", (f.plan.plan_id,
                int(start.timestamp() * 1e6), int(f.state["now"].timestamp() * 1e6), json.dumps(window)))

    result = runtime.sequence(stop=Event(), emit=receive)
    assert result["status"] == "ABORTED"
    assert result["rollback"]["status"] == "CONFIRMED"
    assert result["rollback"]["weight_target"] == 0
    assert len(upstream.patches) == 2
    provider.close()
