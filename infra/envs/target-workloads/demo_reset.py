#!/usr/bin/env python3
"""Check, and where it is safe, restore the target side to the demo's starting state (T28).

Starting state on the target: no demo fault active and the pooler fully up, app writes closed
(write_switch.py status passes and reads "closed"), and the attachments bucket in its
deliberately exposed `open-demo` state so that the S1 gate answers BLOCK first.

`check` only reads. `apply` restores what this repository owns without new credentials:
it clears an active demo fault and closes the target app writes. It never opens the bucket
(that needs the operator's credentials and an explicit acknowledgement: run
`object_exposure.py plan --state open-demo`) and never moves Edge weights (T17).
One JSON line. Exit codes: 0 every checked item is in place, 1 an item is wrong, 3 unconfirmed.
"""
import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

import demo_fault
import object_exposure
import write_switch

PASSED, FAILED, UNKNOWN = 0, 1, 3


def captured(call, *args):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = call(*args)
    lines = stream.getvalue().splitlines()
    return code, json.loads(lines[-1]) if lines else {}


def observe(a, kubectl=None, probe=object_exposure.probe):
    k = ["--kubeconfig", a.kubeconfig] if a.kubeconfig else []
    items = []
    code, fault = captured(lambda: demo_fault.main(["status", "--record", str(a.fault_record), *k], kubectl=kubectl))
    pooler = fault.get("pooler") or {}
    ok = None if code else fault.get("state") == "clear" and pooler.get("ready") == pooler.get("replicas") > 0
    items.append({"name": "demo_fault", "expected": "clear, pooler fully up", "observed": fault.get("state"),
                  "pooler": pooler, "ok": ok})
    code, switch = captured(lambda: write_switch.main(["status", *k]))
    items.append({"name": "target_writes", "expected": "closed", "observed": switch.get("state"),
                  "ok": None if code == 3 else code == 0 and switch.get("state") == "closed"})
    if a.object_endpoint:
        seen = probe(a.object_endpoint, a.bucket, a.probe_key)
        items.append({"name": "attachments_bucket", "expected": "open", "observed": seen["state_observed"],
                      "ok": None if seen["state_observed"] == "unknown" else seen["state_observed"] == "open"})
    else:
        items.append({"name": "attachments_bucket", "expected": "open", "observed": "not_checked", "ok": None})
    items.append({"name": "edge_weights", "expected": "target 0%", "observed": "not_checked (T17)", "ok": None})
    return items


def verdict(items):
    checked = [i for i in items if i["observed"] not in ("not_checked", "not_checked (T17)")]
    if any(i["ok"] is False for i in checked):
        return FAILED
    return UNKNOWN if any(i["ok"] is None for i in checked) else PASSED


def main(argv=None, kubectl=None, probe=object_exposure.probe):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["check", "apply"])
    p.add_argument("--kubeconfig")
    p.add_argument("--fault-record", type=Path, default=Path("demo-fault.json"))
    p.add_argument("--object-endpoint", default="", help="target SeaweedFS S3 endpoint; omit to skip the bucket")
    p.add_argument("--bucket", default="demo-chat-attachments")
    p.add_argument("--probe-key", default="", help="an existing attachment object key")
    a = p.parse_args(argv)
    if a.object_endpoint and not a.probe_key:
        print(json.dumps({"action": a.action, "state": "unchanged", "reason": "--probe-key is required"}))
        return 2
    actions = []
    if a.action == "apply":
        k = ["--kubeconfig", a.kubeconfig] if a.kubeconfig else []
        if a.fault_record.exists():
            code, _ = captured(lambda: demo_fault.main(["clear", "--record", str(a.fault_record), *k], kubectl=kubectl))
            actions.append({"step": "demo_fault clear", "exit_code": code})
        code, switch = captured(lambda: write_switch.main(["status", *k]))
        if switch.get("state") != "closed":
            code, _ = captured(lambda: write_switch.main(["close", *k]))
            actions.append({"step": "write_switch close", "exit_code": code})
    items = observe(a, kubectl, probe)
    code = verdict(items)
    partial = any(i["observed"].startswith("not_checked") for i in items)
    result = {PASSED: "checked_items_ready" if partial else "ready", FAILED: "not_ready", UNKNOWN: "unconfirmed"}[code]
    print(json.dumps({"action": a.action, "result": result,
                      "actions": actions, "items": items}, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
