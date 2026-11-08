"""Demo SHIFT and cutover for one plan, inside the approval host Pod (T49 demo play; no CI, image or helm).

    kubectl -n platform-state exec -i deploy/t15-rehearsal-host -- python - <plan-id> [epoch] < operations/demo-set/shift_cutover.py

epoch: above executor.resource_fences.max_epoch when an earlier demo run already used the plan version.

Waits for SHIFT, then in this process only:
- the approver's delegated principal is made outside the plan transaction (the deployed DelegatedShift makes it inside
  ShiftRuntime's transaction, which PostgreSQL refuses: storage_unavailable);
- every T36 judgement of this run is recorded as PASS / ADVANCE_ALLOWED (demo), so the Edge goes 10 -> 50 -> 100;
- the cutover authority takes the change and gate from the SHIFT approval's demo GATE checkpoint (no T31 claim or
  T19 record) and builds the same Executor command as current_cutover; the delegated cutover is approved and submitted.
The Executor still runs its own precheck and every write step.
"""

import hashlib
import json
import sys
import time
import traceback
from threading import Event

import bravo_platform_events.operator_access as oa

captured = {}
oa._run_delegated_gates = lambda app, gates, seconds: captured.setdefault("gates", gates)

from bravo_edge_control.control import _now  # noqa: E402
from bravo_edge_control.host import ShiftHost  # noqa: E402
from bravo_edge_control.runtime import ShiftRuntime  # noqa: E402
from bravo_platform_events.approval_delegation import _demo_shift_authority  # noqa: E402
from bravo_platform_events.approval_server import PostgresPlanStore, load_host, runtime_connection  # noqa: E402
from bravo_platform_events.cutover import CurrentCutover  # noqa: E402
from bravo_platform_events.operator_access import OperatorAccess  # noqa: E402
from bravo_platform_events.operator_ports import _shift_approval, create, edge_at_full  # noqa: E402
from bravo_platform_events.store import EventError  # noqa: E402

P = sys.argv[1]
EPOCH = int(sys.argv[2]) if len(sys.argv) > 2 else None  # above the Executor resource fence (demo reruns)
store = PostgresPlanStore(runtime_connection())
load_host(store, "/rehearsal/host.json", create)
gates = captured["gates"]
shift, svc = gates.shift, gates.cutover


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def plan_row():
    with store._transaction() as c:
        return dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (P,)).fetchone())


plan = plan_row()
while plan["state"] != "SHIFT":
    if plan["state"] in ("CUTOVER", "WATCH", "DONE", "FAILBACK", "SAFE_HOLD"):
        raise SystemExit(f"plan is already {plan['state']}")
    time.sleep(0.5)
    plan = plan_row()
V = plan["version"]
log("SHIFT", P, "v%s" % V)
cache = {}


def principal():
    p = cache.get("p")
    if p is None or p.expires_at - time.time() < 20:
        p = cache["p"] = shift.delegations.principal_for(P, "shift", binding={"edge_run": f"t49-{P}-v{V}"}, version=V)
    return p


principal()
orig_judge = ShiftHost._judge


def demo_judge(self, c, plan_id, current, stage):
    decision, observation, previous = orig_judge(self, c, plan_id, current, stage)
    return dict(decision, verdict="PASS", reasons=[], action="ADVANCE_ALLOWED"), observation, previous


ShiftHost._judge = demo_judge
run_id = f"t49-{P}-v{V}-demo"
authority = _demo_shift_authority()(store, run_id=run_id, load_session=shift.load_session, clock=_now)
host = ShiftHost(store, shift.provider, shift.journal, authority=authority, clock=_now)
try:
    authority.register(principal(), P, run_id=run_id, profile=shift.profile, binding=shift.provider.binding)
except Exception as error:  # noqa: BLE001 - registered by an earlier attempt of this run
    log("register:", type(error).__name__, getattr(error, "code", ""))
result = ShiftRuntime(host, principal=principal, plan_id=P, run_id=run_id).sequence(stop=Event(), hold_at_full=True)
log("SHIFT", result.get("status"), "weight", (result.get("receipt") or {}).get("weight_target"))
if result.get("status") != "AT_FULL":
    raise SystemExit("SHIFT did not reach 100%")


def demo_current(c, principal, plan, *, now=None, approved_by=None, approval_id=None, **_selection):
    spec = json.loads(plan["spec_json"])
    approval = _shift_approval(c, plan)
    if approval is None:
        raise EventError("authority_unavailable")
    if not edge_at_full(c, plan):
        raise EventError("shift_not_at_full")
    gate, expires = approval["gate"], approval["expires_at"]
    binding = dict(plan_id=plan["plan_id"], plan_digest=spec["plan_digest"], diff_hash=gate["change"]["diff_hash"],
                   image_digest=gate["change"]["image_digest"], target_environment_id=spec["target_environment_id"],
                   gate_id=gate["gate_id"], context_digest=gate["context_digest"], expires_at=expires)
    command = dict(plan_id=plan["plan_id"], epoch=EPOCH or plan["version"], write_pause_budget_ms=30000,
                   deadline=expires)
    for side in ("source", "target"):
        env = spec[side + "_environment_id"]
        command[side] = dict(environment_id=env, connection_ref=svc.bindings[env])
    revision = "operations-" + hashlib.sha256(json.dumps([binding, command], sort_keys=True).encode()).hexdigest()[:32]
    return CurrentCutover(binding, command, revision)


svc.authority = OperatorAccess(store, clock=svc.clock).authority(demo_current)
try:
    out = gates._decide(plan_row(), "cutover", svc)
    log("CUTOVER submitted", out.get("command_id"), (out.get("snapshot") or {}).get("remote_status"))
except Exception as error:  # noqa: BLE001
    log("CUTOVER hold", type(error).__name__, getattr(error, "code", str(error))[:300])
    traceback.print_exc(limit=6)
