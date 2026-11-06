"""Stored T10 phase -> T19 authority -> actual SQLite/PostgreSQL approval path.

Probe evidence and upstream operational permission are synthetic. No cluster
cutover is performed or inferred from these transaction-level integration tests.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_gate_records import (  # noqa: F401 - shared real-store / synthetic-probe fixtures
    _applied_checkpoint,
    bundle,
    contract,
    gate_pg,
    ready,
    records,
    service,
)

from bravo_platform_events.cutover import CurrentCutover, CutoverService
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.store import EventError


@pytest.fixture(params=["initial", "post_apply"])
def flow(request, records):  # noqa: F811
    f = records
    ctx = f.state["context"]
    f.plan = replace(f.plan, plan_id="phase-integration",
                     recheck_profile_id=ctx.profile_id, recheck_profile_version=ctx.profile_version)
    f.store.register_plan(f.plan, run_id="phase-fixture", occurred_at=f.state["now"].isoformat())
    f.records = GateRecords(f.store, authorize=lambda c, p, plan, action:
        f.allowed and p == f.principal and plan["plan_id"] == f.plan.plan_id)
    for side in ("source", "target"):
        f.records.record_run(f.principal, f.plan.plan_id, side, f.state["runs"][side])
    f.phase = request.param
    f.application_id = "applied-1" if f.phase == "post_apply" else None
    if f.application_id:
        _applied_checkpoint(f, application_id=f.application_id)
    f.records.bind(f.principal, f.plan.plan_id, "context", ctx, expected_revision=0,
                   gate_phase=f.phase, application_id=f.application_id)
    assert f.records.evaluate(f.principal, f.plan.plan_id, service_factory=f.factory)["decision"] == "PASS"
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        c.execute("UPDATE plans SET state='SHIFT' WHERE plan_id=?", (f.plan.plan_id,))
    f.version = f.store.snapshot(f.plan.plan_id)["version"]
    expires = (f.state["now"] + timedelta(minutes=5)).isoformat()
    bindings = {ctx.source_environment_id: f.data["binding"].source_database_ref,
                ctx.target_environment_id: f.data["binding"].target_database_ref}
    binding = dict(plan_id=f.plan.plan_id, plan_digest=ctx.plan_digest,
        diff_hash="sha256:" + "c" * 64, image_digest="sha256:" + "d" * 64,
        target_environment_id=ctx.target_environment_id, gate_id=ctx.gate_id,
        context_digest=ctx.fingerprint, expires_at=expires)
    command = dict(plan_id=f.plan.plan_id, epoch=7, write_pause_budget_ms=30000, deadline=expires)
    for side in ("source", "target"):
        env = getattr(ctx, side + "_environment_id")
        command[side] = dict(environment_id=env, connection_ref=bindings[env])
    f.current = CurrentCutover(binding, command, "operations-fixture")
    f.authority = f.records.cutover_authority(upstream=lambda *a, **kw: f.current,
        load_gate=lambda view: (f.factory(view), f.data["binding"]))

    class NoExecutor:
        def submit(self, *a, **kw):
            pytest.fail("approval tests must never dispatch")

        def read(self, *a, **kw):
            pytest.fail("approval tests must never read executor")

    f.cutover = CutoverService(f.store, NoExecutor(), bindings=bindings, authority=f.authority,
                               clock=lambda: f.state["now"].isoformat())
    return f


def current_binding(f):
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        return f.authority(c, f.principal, plan, now=f.state["now"].isoformat(),
                           approved_by=None, approval_id=None).binding


def test_stored_phase_reaches_authority_and_saved_approval_without_manual_copy(flow):
    f = flow
    assert "gate_phase" not in f.current.binding
    binding = current_binding(f)
    assert (binding["gate_phase"], binding["application_id"]) == (f.phase, f.application_id)
    first = f.cutover.approve(f.principal, f.plan.plan_id, binding=binding,
                             expected_version=f.version, command_key="phase-approval")
    again = f.cutover.approve(f.principal, f.plan.plan_id, binding=binding,
                             expected_version=f.version, command_key="phase-approval")
    assert first == again
    assert first["binding"] == binding
    assert "gate_phase" not in f.current.binding  # upstream object was not mutated


@pytest.mark.parametrize("fault", ["missing-phase", "opposite-phase", "other-application"])
def test_client_cannot_relabel_stored_phase_or_application(flow, fault):
    f = flow
    binding = current_binding(f)
    if fault == "missing-phase":
        binding.pop("gate_phase")
    elif fault == "opposite-phase":
        binding.update(gate_phase="post_apply" if f.phase == "initial" else "initial",
                       application_id="applied-1" if f.phase == "initial" else None)
    else:
        binding["application_id"] = "another-application"
    with pytest.raises(EventError):
        f.cutover.approve(f.principal, f.plan.plan_id, binding=binding,
                          expected_version=f.version, command_key="wrong-phase")
    assert f.store.snapshot(f.plan.plan_id)["version"] == f.version


def test_composition_refuses_conflicting_upstream_metadata(flow):
    f = flow
    f.current = replace(f.current, binding=f.current.binding | {"application_id": "wrong-application"})
    with pytest.raises(EventError):
        current_binding(f)


def test_composition_rechecks_current_permissions(flow):
    f = flow
    binding = current_binding(f)
    f.allowed = False
    with pytest.raises(EventError):
        f.cutover.approve(f.principal, f.plan.plan_id, binding=binding,
                          expected_version=f.version, command_key="revoked")
