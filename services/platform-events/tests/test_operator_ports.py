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
                  (plan["plan_id"], f"shift-approval-{max(plan['version'] - 1, 1)}", "{}", json.dumps(shift)))
        first, again = current(c, f.principal, plan), current(c, f.principal, plan)
    assert first == again  # approval and submission compare the whole context
    assert first.binding["diff_hash"] == claim["binding"]["diff_hash"]
    assert (first.binding["gate_id"], first.binding["context_digest"]) == (ctx.gate_id, ctx.fingerprint)
    assert first.command["epoch"] == plan["version"] and first.command["deadline"] == shift["expires_at"]
    assert first.command["target"] == {"environment_id": ctx.target_environment_id, "connection_ref": "cutover/target"}


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
