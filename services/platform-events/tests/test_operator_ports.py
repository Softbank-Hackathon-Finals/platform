"""Cutover command policy: every field from stored records, identical at approval and submission."""

import json

import pytest
from test_gate_cutover_phase import flow  # noqa: F401 - real-store GATE fixture
from test_gate_records import bundle, contract, gate_pg, ready, records, service  # noqa: F401

from bravo_platform_events.operator_ports import current_cutover
from bravo_platform_events.store import EventError


def _plan(c, f):
    return dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())


def test_command_comes_from_the_applied_change_gate_and_shift_approval(flow):  # noqa: F811
    f = flow
    ctx = f.state["context"]
    bindings = {ctx.source_environment_id: "cutover/source", ctx.target_environment_id: "cutover/target"}
    current = current_cutover(bindings)
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = _plan(c, f)
        with pytest.raises(EventError, match="authority_unavailable"):  # nothing applied yet
            current(c, f.principal, plan)
        claim = {"binding": {"diff_hash": "sha256:" + "c" * 64, "image_digest": "sha256:" + "d" * 64}}
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (plan["plan_id"], "change-consumed-x", "{}", json.dumps(claim)))
        with pytest.raises(EventError, match="authority_unavailable"):  # no human SHIFT approval yet
            current(c, f.principal, plan)
        shift = {"expires_at": (f.state["now"].replace(microsecond=0)).isoformat().replace("+00:00", "Z")}
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (plan["plan_id"], f"shift-approval-{plan['version'] - 1}", "{}", json.dumps(shift)))
        with pytest.raises(EventError, match="shift_not_at_full"):  # approved, but the Edge is not at 100%
            current(c, f.principal, plan)
        _edge(c, plan, revision=3, weight=100, verdict="PASS")
        first, again = current(c, f.principal, plan), current(c, f.principal, plan)
    assert first == again  # approval and submission compare the whole context
    assert first.binding["diff_hash"] == claim["binding"]["diff_hash"]
    assert (first.binding["gate_id"], first.binding["context_digest"]) == (ctx.gate_id, ctx.fingerprint)
    assert first.command["epoch"] == plan["version"] and first.command["deadline"] == shift["expires_at"]
    assert first.command["target"] == {"environment_id": ctx.target_environment_id, "connection_ref": "cutover/target"}


def _edge(c, plan, *, revision, weight, verdict, confirmed=None, seq=1):
    """T17 ShiftHost records as the host stores them: a CONFIRMED stage receipt and its judgement."""
    confirmed = confirmed or plan["updated_at"]
    receipt = {"status": "CONFIRMED", "weight_target": weight, "revision": revision, "confirmed_at": confirmed}
    c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
              (plan["plan_id"], f"edge-host-receipt-r{revision}-{seq}", "{}", json.dumps(receipt)))
    c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
              (plan["plan_id"], f"edge-host-judgement-{revision:013}-{seq:013}", "{}",
               json.dumps({"sequence": seq, "judgement": {"verdict": verdict}})))


def test_edge_at_full_needs_the_newest_stage_at_100_with_a_pass(backend):
    from datetime import datetime, timedelta

    from test_failback import NOW, SPEC

    from bravo_platform_events.operator_ports import edge_at_full

    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        entered = datetime.fromisoformat(plan["updated_at"].replace("Z", "+00:00"))
        at = lambda s: (entered + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")  # noqa: E731
        assert not edge_at_full(c, plan)  # no Edge records
        _edge(c, plan, revision=1, weight=50, verdict="PASS", confirmed=at(10))
        assert not edge_at_full(c, plan)  # still at 50%
        _edge(c, plan, revision=2, weight=100, verdict="FAIL", confirmed=at(20))
        assert not edge_at_full(c, plan)  # at 100% but the SLI judgement failed
        _edge(c, plan, revision=2, weight=100, verdict="PASS", confirmed=at(20), seq=2)
        assert edge_at_full(c, plan)  # the newest judgement of the 100% stage passed
        assert not edge_at_full(c, dict(plan, updated_at=at(30)))  # a later SHIFT attempt: older stages never count


def test_policy_needs_both_registered_sites():
    with pytest.raises(ValueError):
        current_cutover({"only": "one"})


def test_failback_reverses_the_committed_cutover_with_a_higher_epoch(backend):
    from test_failback import BINDING, BINDINGS, COMMAND, NOW, SPEC, seed_completed_cutover

    from bravo_platform_events.operator_ports import current_failback

    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    seed_completed_cutover(backend)
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        first = current_failback(BINDINGS)(c, None, plan)
        again = current_failback(BINDINGS)(c, None, plan)
    assert first == again
    assert {k: v for k, v in first.binding.items() if k != "expires_at"} == {k: v for k, v in BINDING.items() if k != "expires_at"}
    assert first.command["epoch"] == 8 and first.command["source"] == COMMAND["source"]
    assert first.command["target"] == COMMAND["target"] and first.command["deadline"] == first.binding["expires_at"]


def test_demo_cutover_takes_the_change_from_the_shift_approval_with_a_rising_epoch(backend):
    from test_failback import NOW, SPEC

    from bravo_platform_events.operator_ports import demo_cutover

    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    current = demo_cutover({SPEC.source_environment_id: "cutover/source", SPEC.target_environment_id: "cutover/target"})
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        with pytest.raises(EventError, match="authority_unavailable"):  # no SHIFT approval
            current(c, None, plan)
        gate = {"change": {"diff_hash": "sha256:" + "c" * 64, "image_digest": "sha256:" + "d" * 64},
                "gate_id": "demo-gate-5", "context_digest": "0" * 64, "verdict": "PASS"}
        shift = {"gate": gate, "approved_at": "2026-11-08T04:05:42Z", "expires_at": "2026-11-08T04:35:42Z"}
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (plan["plan_id"], f"shift-approval-{plan['version'] - 1}", "{}", json.dumps(shift)))
        with pytest.raises(EventError, match="shift_not_at_full"):
            current(c, None, plan)
        _edge(c, plan, revision=3, weight=100, verdict="PASS")
        first, again = current(c, None, plan), current(c, None, plan)
    assert first == again  # approval and submission agree
    assert first.binding["image_digest"] == gate["change"]["image_digest"] and first.binding["gate_id"] == "demo-gate-5"
    assert first.command["epoch"] == 1794110742  # the approval second: above any earlier demo plan's fence
    assert first.command["source"] == {"environment_id": SPEC.source_environment_id, "connection_ref": "cutover/source"}
