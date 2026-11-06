"""Real T10 SQLite/PostgreSQL admission; synthetic probes and remote transport."""
import importlib.util
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_platform_events.cutover import CurrentCutover, CutoverService
from bravo_platform_events.executor import ExecutionRead
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError
from test_readiness import ready  # noqa: F401 - shared synthetic T13/T14 fixture

from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot, fingerprint
from bravo_gate.t10 import PlanGateSession, T10GateAuthority


@pytest.fixture(scope="module")
def cluster():
    # Reuse T10's isolated PG/bootstrap fixture, never an existing database.
    path = Path(__file__).parents[2] / "platform-events/tests/postgres_fixture.py"
    spec = importlib.util.spec_from_file_location("t10_postgres_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pg = module.TemporaryPostgres()
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture
def plan_profile():
    return "context"  # T10 registered the context's profile with the plan


@pytest.fixture
def gate_phase():
    return "initial"


@pytest.fixture(params=["sqlite", "postgres"])
def admission(request, tmp_path, plan_profile, gate_phase):
    gate, state, data, doc, update = request.getfixturevalue("ready")
    if request.param == "postgres":
        pg = request.getfixturevalue("cluster")
        store = PostgresPlanStore(pg.conninfo(pg.database(), "fixture_runtime"))
    else:
        store = PlanStore(tmp_path / "admission.db")
    ctx = state["context"]
    profile = dict(profile_id=ctx.profile_id, profile_version=ctx.profile_version)
    if plan_profile == "legacy":
        profile = {}
    elif plan_profile == "other-id":
        profile["profile_id"] = "full"
    elif plan_profile == "other-version":
        profile["profile_version"] = "2"
    recheck = dict(recheck_profile_id=ctx.profile_id, recheck_profile_version=ctx.profile_version) if profile else {}
    if plan_profile == "recheck-missing":
        recheck = {}
    elif plan_profile == "recheck-other-id":
        recheck["recheck_profile_id"] = "full"
    elif plan_profile == "recheck-other-version":
        recheck["recheck_profile_version"] = "2"
    plan = PlanSpec("plan-demo", ctx.tenant_id, ctx.app, ctx.source_environment_id,
                    ctx.target_environment_id, ctx.plan_digest, "b" * 64, **profile,
                    **recheck)
    store.register_plan(plan, run_id="fixture", occurred_at=state["now"].isoformat())
    with store._transaction(write=True, plan_id=plan.plan_id) as c:
        c.execute("UPDATE plans SET state='SHIFT' WHERE plan_id=?", (plan.plan_id,))
    envs = {ctx.source_environment_id: data["binding"].source_database_ref,
            ctx.target_environment_id: data["binding"].target_database_ref}
    expires = (state["now"] + timedelta(minutes=10)).isoformat()
    binding = dict(plan_id=plan.plan_id, plan_digest=ctx.plan_digest, diff_hash="sha256:" + "c" * 64,
                   image_digest="sha256:" + "d" * 64, target_environment_id=ctx.target_environment_id,
                   gate_id=ctx.gate_id, context_digest=ctx.fingerprint, expires_at=expires,
                   gate_phase=gate_phase, application_id="applied-1" if gate_phase == "post_apply" else None)
    command = dict(plan_id=plan.plan_id, epoch=7, write_pause_budget_ms=30000, deadline=expires)
    for side in ("source", "target"):
        env = getattr(ctx, side + "_environment_id")
        command[side] = dict(environment_id=env, connection_ref=envs[env])
    f = SimpleNamespace(gate=gate, state=state, data=data, doc=doc, update=update, store=store,
                        plan=plan, principal=Principal("operator", ctx.tenant_id), requests=[],
                        current=CurrentCutover(binding, command, "operations-v1"),
                        upstream_allowed=True, hook=lambda: None, calls=0, connections=[], report_revision=0,
                        gate_phase=gate_phase, application_id=binding["application_id"])

    def publish():
        report = gate.evaluate("operator", "context")
        f.report_revision += 1
        with store._transaction(write=True, plan_id=plan.plan_id) as c:
            c.execute("INSERT INTO plan_commands VALUES (?, ?, '{}', ?)",
                      (plan.plan_id, f"fixture-gate-{f.report_revision:08d}", canonical(report).decode()))
        return report
    f.publish = publish
    publish()

    def upstream(c, principal, p, **_):
        f.calls += 1
        f.connections.append(c)
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (p["plan_id"],)).fetchone()["state"] == "SHIFT"
        if not f.upstream_allowed:
            raise EventError("forbidden")
        f.hook()
        return f.current

    def load(c, principal, p):
        assert c is f.connections[-1]
        row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? ORDER BY command_key DESC LIMIT 1",
                        (p["plan_id"], "fixture-gate-%")).fetchone()
        if row is None:
            return None
        f.last_session = PlanGateSession(gate, p["plan_id"], "context", state["context"],
            RunSnapshot(state["context"].gate_id, row["receipt_json"].encode()), data["binding"],
            gate_phase=f.gate_phase, application_id=f.application_id)
        return f.last_session
    f.loader = load
    f.adapter = T10GateAuthority(upstream=upstream, load_session=lambda *args: f.loader(*args))

    class Remote:
        def submit(self, expected, command, *, idempotency_key):
            assert store.snapshot(plan.plan_id)["state"] == "CUTOVER"
            f.requests.append(("POST", command))
            return ExecutionRead("UNKNOWN", "fixture_response_loss")

        def read(self, *args, **kwargs):
            f.requests.append(("GET", None))
            return ExecutionRead("UNKNOWN", "fixture_response_loss")
    f.service = CutoverService(store, Remote(), bindings=envs, authority=f.adapter,
                               clock=lambda: state["now"].isoformat())
    f.approve = lambda: f.service.approve(f.principal, plan.plan_id, binding=f.current.binding,
                                         expected_version=1, command_key="approve")
    f.submit = lambda approval: f.service.submit(f.principal, plan.plan_id,
        approval_id=approval["approval_id"], command_key="submit")
    return f


def test_approved_dispatch_and_response_loss_retries_only_read(admission):
    f = admission
    approved = f.approve()
    f.state["now"] += timedelta(seconds=6)  # Revalidate, do not freeze a five-second proof.
    assert f.submit(approved)["outcome"] == "UNKNOWN"
    assert f.store.snapshot(f.plan.plan_id)["state"] == "SAFE_HOLD"
    f.submit(approved)
    assert [method for method, _ in f.requests] == ["POST", "GET"]


@pytest.mark.parametrize("gate_phase", ["initial", "post_apply"])
def test_phase_bound_approval_dispatches_once(admission, gate_phase):
    f = admission
    approved = f.approve()
    assert approved["binding"]["gate_phase"] == gate_phase
    assert approved["binding"]["application_id"] == f.application_id
    f.submit(approved)
    f.submit(approved)
    assert [method for method, _ in f.requests] == ["POST", "GET"]


@pytest.mark.parametrize("gate_phase", ["initial", "post_apply"])
def test_approval_cannot_cross_phase_in_either_direction(admission, gate_phase):
    f = admission
    approved = f.approve()
    f.gate_phase = "post_apply" if gate_phase == "initial" else "initial"
    f.application_id = "applied-1" if f.gate_phase == "post_apply" else None
    f.current = replace(f.current, binding=f.current.binding | {
        "gate_phase": f.gate_phase, "application_id": f.application_id})
    with pytest.raises(EventError):
        f.submit(approved)
    assert f.requests == []
    assert f.store.snapshot(f.plan.plan_id)["state"] == "SHIFT"


@pytest.mark.parametrize("gate_phase", ["post_apply"])
def test_approval_cannot_be_reused_for_another_application(admission, gate_phase):
    f = admission
    approved = f.approve()
    f.application_id = "applied-2"
    f.current = replace(f.current, binding=f.current.binding | {"application_id": f.application_id})
    with pytest.raises(EventError):
        f.submit(approved)
    assert not f.requests


@pytest.mark.parametrize("fault", ["phase-missing", "application-missing", "phase-mismatch", "application-mismatch"])
def test_missing_or_mismatched_binding_phase_refuses_before_approval(admission, fault):
    f = admission
    if fault.endswith("missing"):
        f.current.binding.pop("gate_phase" if fault == "phase-missing" else "application_id")
    else:
        f.current.binding["gate_phase" if fault == "phase-mismatch" else "application_id"] = (
            "post_apply" if fault == "phase-mismatch" else "applied-1")
    with pytest.raises(EventError):
        f.approve()
    assert not f.requests and len(f.store.read_after(f.plan.plan_id, None)) == 1


def test_session_fingerprint_binds_phase_and_application(admission):
    f = admission
    f.approve()
    initial = f.last_session
    assert initial.fingerprint == fingerprint(dict(plan_id=initial.plan_id,
        context_id=initial.context_id, context=initial.context.fingerprint,
        report_ref=initial.report.ref, report=initial.report.fingerprint,
        readiness=initial.readiness.fingerprint, approval_ids=initial.approval_ids))
    after = replace(initial, gate_phase="post_apply", application_id="applied-1")
    another = replace(after, application_id="applied-2")
    assert len({initial.fingerprint, after.fingerprint, another.fingerprint}) == 3
    for phase, app in (("initial", "applied-1"), ("post_apply", None), ("unknown", None), ([], None)):
        with pytest.raises(GateError, match="INVALID_PLAN_GATE_SESSION"):
            replace(initial, gate_phase=phase, application_id=app)


@pytest.mark.parametrize("gate_phase", ["initial", "post_apply"])
def test_legacy_binding_is_initial_only(admission, gate_phase):
    f = admission
    f.current.binding.pop("gate_phase")
    f.current.binding.pop("application_id")
    if gate_phase == "initial":
        approved = f.approve()
        assert "gate_phase" not in approved["binding"]
    else:
        with pytest.raises(EventError):
            f.approve()
        assert not f.requests


@pytest.mark.parametrize("gate_phase", ["post_apply"])
@pytest.mark.parametrize("plan_profile", ["other-id"])
def test_post_apply_selects_recheck_profile_instead_of_initial(admission, gate_phase, plan_profile):
    assert admission.approve()["binding"]["gate_phase"] == "post_apply"


@pytest.mark.parametrize("gate_phase", ["post_apply"])
@pytest.mark.parametrize("plan_profile", ["recheck-missing", "recheck-other-id", "recheck-other-version"])
def test_post_apply_requires_its_registered_recheck_profile(admission, gate_phase, plan_profile):
    with pytest.raises(EventError):
        admission.approve()
    assert not admission.requests


@pytest.mark.parametrize("fault", ["missing", "data-block", "security-revoked", "expired", "operational-denial"])
def test_bad_gate_never_creates_approval_or_calls_executor(admission, fault):
    f = admission
    if fault == "missing":
        f.loader = lambda *_: None
    elif fault == "data-block":
        f.doc["target"]["tables"][0]["schema_hash"] = "c" * 64
        f.update(f.doc)
        assert f.publish()["decision"] == "BLOCK"
    elif fault == "security-revoked":
        f.state["allowed"] = False
    elif fault == "expired":
        f.state["now"] += timedelta(seconds=61)
    else:
        f.upstream_allowed = False
    with pytest.raises(EventError):
        f.approve()
    assert f.requests == [] and f.store.snapshot(f.plan.plan_id)["state"] == "SHIFT"
    assert len(f.store.read_after(f.plan.plan_id, None)) == 1


@pytest.mark.parametrize("fault", ["report", "readiness", "security", "operation", "expired"])
def test_old_approval_cannot_dispatch_after_authority_change(admission, fault):
    f = admission
    approval = f.approve()
    if fault == "report":
        f.state["now"] += timedelta(seconds=1)
        f.publish()  # Same PASS, different registered report: must require a new approval.
    elif fault == "readiness":
        f.doc["target"]["tables"][0]["schema_hash"] = "c" * 64
        f.update(f.doc)
    elif fault == "security":
        f.state["trusted"] = False
    elif fault == "operation":
        f.upstream_allowed = False
    else:
        f.state["now"] += timedelta(seconds=31)
    with pytest.raises(EventError):
        f.submit(approval)
    assert not f.requests and f.store.snapshot(f.plan.plan_id)["state"] == "SHIFT"


@pytest.mark.parametrize("fault", ["plan", "app", "tenant", "gate", "context", "database", "environment"])
def test_gate_cannot_be_reused_for_different_plan_resources(admission, fault):
    f = admission
    loader = f.loader
    if fault == "plan":
        f.loader = lambda *args: replace(loader(*args), plan_id="different-plan")
    elif fault in {"app", "tenant"}:
        context = replace(f.state["context"], **(
            {"app": "other-app"} if fault == "app" else {"tenant_id": "other"}))
        f.loader = lambda *args: replace(loader(*args), context=context,
            readiness=replace(f.data["binding"], context_digest=context.fingerprint))
    elif fault in {"gate", "context"}:
        f.current.binding["gate_id" if fault == "gate" else "context_digest"] = "other"
    else:
        f.current.command["source"]["connection_ref" if fault == "database" else "environment_id"] = "other"
    with pytest.raises(EventError):
        f.approve()
    assert not f.requests


@pytest.mark.parametrize("plan_profile", ["legacy", "other-id", "other-version"])
def test_plan_profile_must_be_the_bound_context_profile(admission, plan_profile):
    # The context and its binding are valid; only the plan T10 registered carries another or no profile.
    with pytest.raises(EventError):
        admission.approve()
    assert not admission.requests


def test_slow_second_operational_check_cannot_extend_gate_proof(admission):
    f = admission
    def slow():
        if f.calls == 2:
            f.state["now"] += timedelta(seconds=6)
    f.hook = slow
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()
    assert not f.requests


def test_mutable_operational_record_cannot_rewrite_first_observation(admission):
    f = admission
    def mutate():
        if f.calls == 2:
            f.current.binding["image_digest"] = "sha256:" + "f" * 64
    f.hook = mutate
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()
    assert not f.requests


def test_explicit_host_providers_required():
    with pytest.raises(GateError, match="EXPLICIT_SERVER_AUTHORITY_REQUIRED"):
        T10GateAuthority(upstream=None, load_session=None)


@pytest.mark.parametrize("fault", ["report-replaced", "loader-failure", "revision", "clock"])
def test_changed_or_unavailable_host_authority_blocks(admission, fault):
    f = admission
    loader = f.loader
    reads = 0

    def load(*args):
        nonlocal reads
        reads += 1
        session = loader(*args)
        if fault == "loader-failure":
            raise RuntimeError("fixture-private-provider-detail")
        if fault == "report-replaced" and reads == 2:
            return replace(session, report=RunSnapshot(session.report.ref, b'{"changed":true}'))
        return session

    f.loader = load
    if fault == "revision":
        f.current = replace(f.current, revision=None)
    if fault == "clock":
        f.hook = lambda: f.state.update(now=f.state["now"] - timedelta(seconds=1))
    with pytest.raises(EventError, match="^authority_unavailable$"):
        f.approve()
    assert not f.requests
    assert f.store.snapshot(f.plan.plan_id)["state"] == "SHIFT"


def test_cross_tenant_request_never_reads_gate_records(admission):
    f = admission
    f.principal = Principal("operator", "unrelated-tenant")
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    assert f.calls == 0 and not f.requests


def _stale_report(f, hours=2):
    report = json.loads(canonical(f.gate.evaluate("operator", "context")))
    report["evaluated_at"] = (f.state["now"] - timedelta(hours=hours)).isoformat()
    f.report_revision += 1
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        c.execute("INSERT INTO plan_commands VALUES (?, ?, '{}', ?)",
                  (f.plan.plan_id, f"fixture-gate-{f.report_revision:08d}", canonical(report).decode()))


def test_cutover_approval_reevaluates_a_stale_gate_report(admission):
    """#532 (sangmu1126 15:21Z) for the cutover authority: hours after GATE, a fresh PASS bound to the
    same context and readiness admits; the GATE report stays and the fresh one is kept beside it."""
    f = admission
    _stale_report(f)
    approved = f.approve()
    assert approved["approval_id"]
    with f.store._transaction() as c:
        kept = c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("gate-cutover-reevaluation-%",)).fetchone()["n"]
        gates = c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                          ("fixture-gate-%",)).fetchone()["n"]
    assert kept >= 1 and gates == 2


def test_cutover_approval_stops_when_the_fresh_gate_blocks(admission, monkeypatch):
    f = admission
    _stale_report(f)
    original = f.gate.evaluate
    monkeypatch.setattr(f.gate, "evaluate", lambda *a, **k: original(*a, **k) | {"decision": "BLOCK"})
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()
