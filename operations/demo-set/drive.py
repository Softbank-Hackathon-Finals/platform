"""Demo driver: one plan from COLLECT to DONE without CI, image publishing or helm (T49 demo play, < 10 min).

    kubectl -n platform-state exec -i deploy/t10-workflow-host -- python - <plan-id> <image-digest> < operations/demo-set/drive.py

Runs inside the workflow host Pod with the deployed platform code. For this one plan it passes the checking
stages on the platform's own evidence through the same transition (WorkflowRecovery.resume_once), with the
checks off in this process only: COLLECT (no T16 scan), ANALYZE (no T14), PROPOSE (the given image, already
running on the destination; no AI or build), REHEARSE (no T15/T31), GATE (no T19). AWAIT_APPROVAL, SHIFT and
CUTOVER stay with the hosts (auto delegation, Edge, Executor). WATCH passes at once (no retest, so no failback).
"""

import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime

import bravo_platform_events.workflow as wf
from bravo_platform_events.cutover import CutoverService, _put
from bravo_platform_events.failback import cutover_checkpoint
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import CollectionProof
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_server import runtime_connection
from bravo_platform_events.workflow import (
    Analysis,
    Application,
    Change,
    Evidence,
    Gate,
    Proposal,
    Run,
    Watch,
    WorkflowRecovery,
)

PLAN, IMAGE = sys.argv[1], sys.argv[2]
CHANGE = Change("sha256:" + hashlib.sha256(f"demo:{IMAGE}".encode()).hexdigest(), IMAGE)
LIMIT = time.monotonic() + 20 * 60
store = PostgresPlanStore(runtime_connection())
wf._satisfied = lambda *a, **k: True  # this process only: the demo stages are not checked
wf.require_rehearsals_complete = lambda c, plan: None


def log(*parts):
    print(datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3], *parts, flush=True)


def read(c, plan, previous, run):
    spec, state = json.loads(plan["spec_json"]), plan["state"]
    tag = f"demo-{state.lower()}-{plan['version']}"
    if state == "ANALYZE":
        result = Analysis(spec["collection_scope_digest"], "0" * 64, "0" * 64)
    elif state == "PROPOSE":
        result = Proposal("0" * 64, "0" * 64, CHANGE, True, True)
    elif state in ("REHEARSE", "GATE"):
        carried = Change(**previous["result"]["change"])
        result = (Application(carried, tag, tag, tag, True, carried.image_digest) if state == "REHEARSE"
                  else Gate(carried, tag, "0" * 64, "PASS"))
    else:
        raise RuntimeError("not a demo state: " + state)
    return Evidence(plan["plan_id"], spec["plan_digest"], spec["target_environment_id"], plan["version"], tag, result)


def watch_done():
    with store._transaction(write=True, plan_id=PLAN) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (PLAN,)).fetchone())
        if plan["state"] != "WATCH":
            return
        spec, version, now = json.loads(plan["spec_json"]), plan["version"], datetime.now(UTC).isoformat()
        previous = cutover_checkpoint(c, PLAN)[1]
        tag = f"demo-watch-{version}"
        proof = Evidence(PLAN, spec["plan_digest"], spec["target_environment_id"], version, tag,
                         Watch(Run(**previous["result"]["run"]), previous["receipt_id"], "PASS", "PASS"))
        _put(c, PLAN, f"workflow:{version}", {}, asdict(proof))
        c.execute("UPDATE plans SET state='DONE',version=version+1,updated_at=? WHERE plan_id=?", (now, PLAN))
        store._append_in_transaction(c, plan_id=PLAN, run_id=tag, event_type="state.changed", occurred_at=now,
                                     producer_key=f"workflow-transition-{version}", data={"from": "WATCH", "to": "DONE"},
                                     state_owner=True)


recovery = WorkflowRecovery(CutoverService(store, object(), bindings={"env-aws": "source", "env-onprem": "target"}),
                            read_evidence=read)
principal, last = Principal("workflow-host", "bravo"), None
log("start", PLAN, IMAGE)
while time.monotonic() < LIMIT:
    with store._transaction() as c:
        row = dict(c.execute("SELECT state, version, spec_json FROM plans WHERE plan_id=?", (PLAN,)).fetchone())
    state = row["state"]
    if (state, row["version"]) != last:
        log("state", state, "v%s" % row["version"])
        last = (state, row["version"])
    try:
        if state == "COLLECT":
            spec = json.loads(row["spec_json"])
            store.complete_collection(CollectionProof(PLAN, spec["plan_digest"], spec["collection_scope_digest"],
                                                      "demo-collect-" + PLAN[5:21], True),
                                      expected_version=row["version"], command_key="demo-collect",
                                      run_id="demo-collect", occurred_at=datetime.now(UTC).isoformat())
        elif state in ("ANALYZE", "PROPOSE", "REHEARSE", "GATE"):
            recovery.resume_once(principal, PLAN)
        elif state == "WATCH":
            watch_done()
        elif state in ("DONE", "FAILBACK", "ROLLED_BACK", "SAFE_HOLD"):
            log("end", state)
            break
    except Exception as error:  # noqa: BLE001 - report and keep driving; the hosts own the other states
        log("hold", state, type(error).__name__, getattr(error, "code", str(error))[:160])
        time.sleep(2)
    time.sleep(0.5)
