"""The plan's RunState (T49): one owner per state, no silent unwired state, and what is owed by whom."""

import json
from datetime import timedelta

import pytest
from test_approval_delegation import NOW, SPEC, T0, delegate, person, world  # noqa: F401  (fixture)

from bravo_platform_events.plan_run_state import OWNERS, analysis_reader, check_wiring, run_state
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import STAGES, Analysis


def test_a_host_state_is_driven_or_declared_deferred_never_silently_unwired():
    every = {s: (lambda *a: None) for s in STAGES}
    assert check_wiring(every, {}) == []
    with pytest.raises(ValueError, match="workflow_state_unwired:ANALYZE,WATCH"):
        check_wiring({s: f for s, f in every.items() if s not in ("ANALYZE", "WATCH")}, {})
    assert check_wiring({s: f for s, f in every.items() if s != "WATCH"}, {}, deferred=["WATCH"]) == ["WATCH"]
    with pytest.raises(ValueError, match="workflow_deferred_unknown_state"):
        check_wiring(every, {}, deferred=["SHIFT"])  # SHIFT is not the host's to defer
    assert all(OWNERS[s][0] == "host" for s in STAGES)


def _plan(store, state):
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("UPDATE plans SET state=? WHERE plan_id=?", (state, SPEC.plan_id))
        return dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())


def test_the_run_state_names_the_owed_decision_and_who_makes_it(world):  # noqa: F811
    store, d = world["store"], world["delegations"]
    with store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        assert run_state(c, plan, delegation=d.live(c, SPEC.plan_id))["interruptions"] == []
    plan = _plan(store, "SHIFT")
    with store._transaction() as c:
        state = run_state(c, plan, delegation=d.live(c, SPEC.plan_id))
    assert (state["owner"], state["interruptions"]) == ("approval-host", [
        {"gate": "cutover", "decides": "person", "approver": None}])
    delegate(world)
    with store._transaction() as c:
        state = run_state(c, plan, delegation=d.live(c, SPEC.plan_id))
    assert state["interruptions"] == [{"gate": "cutover", "decides": "delegation", "approver": "approver"}]
    json.dumps(state)  # serializable, as RunState.to_json
    world["clock"].now += timedelta(hours=5)  # the delegation expired: a person decides again
    with store._transaction() as c:
        assert run_state(c, plan, delegation=d.live(c, SPEC.plan_id))["interruptions"][0]["decides"] == "person"


def test_analyze_reads_only_the_s1_records_of_the_initial_context(world):  # noqa: F811
    from bravo_platform_events.gate_records import _key

    store = world["store"]
    plan = _plan(store, "ANALYZE")
    spec = json.loads(plan["spec_json"])
    with store._transaction() as c, pytest.raises(EventError, match="analysis_collection_unavailable"):
        analysis_reader(c, plan, None, None)
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (SPEC.plan_id, "collection-original:t16-x", "{}", json.dumps(
            {"collection_scope_digest": spec["collection_scope_digest"], "inventory": {"digest": "sha256:" + "a" * 64}})))
    with store._transaction() as c, pytest.raises(EventError, match="analysis_gate_unavailable"):
        analysis_reader(c, plan, None, None)
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (SPEC.plan_id, "gate-current-0000000000001", "{}",
                                                                 json.dumps({"context": {"gate_id": "g-initial"}, "revision": 1})))
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (SPEC.plan_id, _key("security-report", "g-initial"), "{}",
                                                                 json.dumps({"decision": "BLOCK"})))
    with store._transaction() as c:
        proof = analysis_reader(c, plan, None, None)
    assert isinstance(proof.result, Analysis) and proof.result.inventory_digest == "a" * 64
    assert proof.result.scope_digest == spec["collection_scope_digest"] and proof.receipt_id.startswith("t14-analysis-")
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:  # a post-apply context is not S1 evidence
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (SPEC.plan_id, "gate-current-0000000000002", "{}", json.dumps(
            {"context": {"gate_id": "g-post"}, "revision": 2, "gate_phase": "post_apply"})))
    with store._transaction() as c, pytest.raises(EventError, match="analysis_gate_unavailable"):
        analysis_reader(c, plan, None, None)


def test_the_approval_host_starts_the_one_rehearsal_and_moves_a_failback_on(world):  # noqa: F811
    from bravo_platform_events.approval_delegation import DelegatedGates

    calls = []

    class Rehearsals:
        def start(self, principal, plan_id, **kw):
            calls.append(("start", principal.subject_id, kw["task_id"], kw["command_key"]))
            return {"rehearsal_id": "r-1"}

    class Failback:
        def reconcile(self, principal, plan_id, command_id):
            calls.append(("reconcile", command_id))
            return {"state": "FAILBACK"}

        def advance(self, principal, plan_id, command_id):
            calls.append(("advance", command_id))
            return "rolled_back"

    store, tasks = world["store"], {"value": ["credential-migration"]}
    gates = DelegatedGates(store, world["delegations"], cutover=None, failback=Failback(), rehearsal=Rehearsals(),
                           list_tasks=lambda plan_id: tasks["value"])
    plan = _plan(store, "REHEARSE")
    assert gates.sweep() == {}  # nothing delegated: a person starts it
    delegate(world, gates=["shift"], bounds={})
    assert gates.sweep() == {}  # delegating SHIFT never starts a rehearsal
    world["delegations"].revoke(world["approver"], SPEC.plan_id)
    world["access"].set_permissions(SPEC.plan_id, person("approver", T0), permissions=[
        "read", "approve_shift", "approve_cutover", "start_rehearsal", "approve_failback"], expected_revision=1,
        administration_ref="fixture", valid_after=NOW, expires_at=(T0 + timedelta(hours=12)).isoformat(),
        command_key="grant-2")
    world["approver"] = person("approver", world["clock"].now)
    delegate(world, gates=["rehearsal", "failback"], bounds={}, command_key="delegate-r")
    tasks["value"] = ["a", "b"]
    assert gates.sweep()[SPEC.plan_id] == {"held": "rehearsal_task_unselected"}  # S1 chooses the task
    tasks["value"] = ["credential-migration"]
    assert gates.sweep()[SPEC.plan_id] == {"rehearsal_id": "r-1"}
    assert calls[-1] == ("start", "approver", "credential-migration", f"delegated-rehearsal-{plan['version']}")
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:  # admitted: never started twice
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (SPEC.plan_id, f"rehearsal:{plan['version']}:credential-migration:1", "{}", "{}"))
    assert gates.sweep() == {}
    plan = _plan(store, "FAILBACK")
    from bravo_platform_events.failback import failback_key

    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (SPEC.plan_id, failback_key("active", str(plan["version"])),
                                                                 "{}", json.dumps({"command_id": "fb-1"})))
    assert gates.sweep()[SPEC.plan_id] == {"state": "FAILBACK"} and calls[-1] == ("reconcile", "fb-1")
