"""Shared Executor observation projection: the running state is a parameter (T20 spec commit 2).

The cutover path keeps its behaviour (tests/test_cutover.py). These tests drive the same projection
for a non-cutover running state with a separate key namespace, as the failback service will.
"""

import hashlib

import pytest

from bravo_platform_events.cutover import _put, project_observation
from bravo_platform_events.executor import ExecutionRead, ExecutionSnapshot, ExecutionStep
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec

NOW = "2026-11-04T10:00:00+00:00"
PRINCIPAL = Principal("operator", "tenant-demo")
SPEC = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-abcdefabcdef"


def probe_key(kind, value):
    return f"probe-{kind}-" + hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def plan(backend):
    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with backend.admin() as c:
        c.execute("UPDATE plans SET state='FAILBACK' WHERE plan_id='plan-demo'")
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        _put(c, SPEC.plan_id, probe_key("command", COMMAND_ID), {}, dict(command=dict(epoch=8)))
    return backend


def snapshot(status, committed, steps=("precheck",)):
    return ExecutionSnapshot(EXECUTION, SPEC.plan_id, 8, status, committed,
                             tuple(ExecutionStep(s, "succeeded", 1) for s in steps),
                             "2026-11-04T10:00:00+00:00", "2026-11-04T10:00:01+00:00")


def project(backend, read, **options):
    return project_observation(backend.reopen(), PRINCIPAL, SPEC.plan_id, COMMAND_ID, read, check_access=None,
                               now=NOW, key_for=probe_key, running_state="FAILBACK", **options)


def state(backend):
    return backend.reopen().snapshot(SPEC.plan_id)["state"]


@pytest.mark.parametrize("read", [ExecutionRead("UNKNOWN", "timeout"),
                                  ExecutionRead("OBSERVED", snapshot=snapshot("SAFE_HOLD", True))])
def test_uncertain_observation_holds_the_given_running_state(plan, read):
    project(plan, read, hold_after_done=False)
    assert state(plan) == "SAFE_HOLD"
    changes = [e["data"] for e in plan.store.read_after(SPEC.plan_id, None) if e["event_type"] == "state.changed"]
    assert changes[-1] == {"from": "FAILBACK", "to": "SAFE_HOLD"}


def test_stored_committed_done_is_final_when_requested(plan):
    project(plan, ExecutionRead("OBSERVED", snapshot=snapshot("DONE", True)), hold_after_done=False)
    project(plan, ExecutionRead("UNKNOWN", "timeout"), hold_after_done=False)
    assert state(plan) == "FAILBACK"


def test_default_keeps_the_cutover_rule_that_unknown_after_done_still_holds(plan):
    project(plan, ExecutionRead("OBSERVED", snapshot=snapshot("DONE", True)))
    project(plan, ExecutionRead("UNKNOWN", "timeout"))
    assert state(plan) == "SAFE_HOLD"


def test_other_states_and_other_key_namespaces_are_not_touched(plan):
    with plan.admin() as c:
        c.execute("UPDATE plans SET state='WATCH' WHERE plan_id='plan-demo'")
    project(plan, ExecutionRead("UNKNOWN", "timeout"), hold_after_done=False)
    assert state(plan) == "WATCH"
    # The cutover journal never sees this command: its key namespace differs.
    with pytest.raises(Exception, match="command_not_found"):
        project_observation(plan.reopen(), PRINCIPAL, SPEC.plan_id, COMMAND_ID, ExecutionRead("UNKNOWN", "x"),
                            check_access=None, now=NOW, key_for=lambda k, v: f"cutover-{k}-{v}",
                            running_state="CUTOVER")
