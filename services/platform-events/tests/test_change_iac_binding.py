"""ADR-0044: a change approval binds the one accepted agent infra.plan that closes the attachment bucket."""

import copy
import json
from pathlib import Path

import bravo_iac
import pytest
from test_cutover import NOW, SPEC

from bravo_platform_events.change import change_binding, intake_binding
from bravo_platform_events.cutover import _put
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow_propose import apply_input_key

STATE = json.loads((Path(__file__).resolve().parents[3] / "infra/iac/environments/target-dev.json").read_text())
DIFF = "sha256:" + "a" * 64


def exposure(state, value):
    out = copy.deepcopy(state)
    module = next(m for m in out["modules"] if m["id"] == "target-object-exposure")
    module["params"] = {k: v for k, v in module["params"].items() if k != "demo_run_id"} | {"state": value}
    if value == "open-demo":
        module["params"]["demo_run_id"] = "demo-2026-11-08"
    return out


def record():
    candidate = {"plan_id": SPEC.plan_id, "target_environment_id": SPEC.target_environment_id,
                 "diff_hash": DIFF, "image_digest": "sha256:" + "b" * 64}
    return {"candidate": candidate, "plan_digest": SPEC.plan_digest, "expires_at": "2026-11-08T03:00:00+00:00"}


def binding_with(backend, plans):
    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        if plans is not None:
            _put(c, SPEC.plan_id, apply_input_key(DIFF), {}, {"edits": [], "diff_b64": "", "diff_hash": DIFF,
                                                               "infra_plans": plans})
        return change_binding(c, record())


def test_the_closing_plan_is_bound_and_nothing_else_changes(backend):
    opened = exposure(STATE, "open-demo")
    closing = bravo_iac.plan(opened, exposure(STATE, "closed"), origin="agent")
    assert closing["accepted"]
    binding = binding_with(backend, [closing])
    assert binding["iac_plan_digest"] == closing["plan_digest"]
    assert {k: v for k, v in binding.items() if k != "iac_plan_digest"} == intake_binding(record())


@pytest.mark.parametrize("plans", [None, [], "no-close", "widening", "tampered"])
def test_without_a_valid_closing_plan_the_binding_is_the_candidates(backend, plans):
    opened, closed = exposure(STATE, "open-demo"), exposure(STATE, "closed")
    if plans == "no-close":
        plans = [bravo_iac.plan(opened, opened, origin="agent")]
    elif plans == "widening":
        plans = [bravo_iac.plan(closed, opened, origin="agent")]  # refused: IAC_WIDENING_REFUSED
    elif plans == "tampered":
        forged = bravo_iac.plan(opened, closed, origin="agent")
        forged["base_digest"] = "0" * 64  # body no longer matches plan_digest
        plans = [forged]
    binding = binding_with(backend, plans)
    assert "iac_plan_digest" not in binding
    assert set(binding) == {"plan_id", "target_environment_id", "diff_hash", "image_digest", "plan_digest", "expires_at"}


def test_the_same_closing_plan_twice_is_one_plan(backend):
    agent = bravo_iac.plan(exposure(STATE, "open-demo"), exposure(STATE, "closed"), origin="agent")
    assert binding_with(backend, [agent, agent])["iac_plan_digest"] == agent["plan_digest"]


def test_two_different_closing_plans_fail_closed(backend):
    opened, closed = exposure(STATE, "open-demo"), exposure(STATE, "closed")
    agent = bravo_iac.plan(opened, closed, origin="agent")
    operator = bravo_iac.plan(opened, closed, origin="operator")
    assert agent["plan_digest"] != operator["plan_digest"] and operator["accepted"]
    with pytest.raises(EventError, match="iac_plan_ambiguous"):
        binding_with(backend, [agent, operator])
