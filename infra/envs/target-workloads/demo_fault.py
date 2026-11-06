#!/usr/bin/env python3
"""S4 demo fault: take the target DB path down by scaling the target pooler to 0, then restore it.

`inject` records the pooler Deployment (UID, replicas) before any change, then scales it to 0
only if it still has exactly that many replicas (`--current-replicas`). Target app requests that
touch the DB then fail with 5xx, which the T18 windows count and SHIFT/WATCH judge. Replication
connects to the databases directly, so it keeps running. `clear` restores the recorded replicas
on the same Deployment UID and waits for the rollout. Clear the fault before approving FAILBACK:
the cutover executor reconfigures the pooler during FAILBACK.
One JSON line goes to stdout. Exit codes: 0 passed, 1 wrong state, 2 input error, 3 unconfirmed.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from render import NAMESPACE

PASSED, FAILED, INPUT_ERROR, UNKNOWN = 0, 1, 2, 3
POOLER = "target-pgbouncer"


class Unknown(Exception):
    pass


def kubectl_with(kubeconfig):
    def kubectl(*args, timeout=30):
        command = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "-n", NAMESPACE, *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown(f"kubectl {args[0]} could not run") from None
        if result.returncode:
            raise Unknown(f"kubectl {args[0]} failed")  # stderr may quote objects; not echoed
        return result.stdout
    return kubectl


def pooler(kubectl):
    d = json.loads(kubectl("get", "deployment", POOLER, "-o", "json"))
    status = d.get("status", {})
    return {"uid": d["metadata"]["uid"], "replicas": d["spec"].get("replicas", 1),
            "ready": status.get("readyReplicas", 0), "generation": d["metadata"]["generation"],
            "observed": status.get("observedGeneration")}


def report(action, state, code, **extra):
    print(json.dumps({"action": action, "state": state, "observed_at": datetime.now(UTC).isoformat(), **extra},
                     sort_keys=True))
    return code


def write_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def inject(kubectl, record, wait):
    if record.exists():
        return report("inject", "unchanged", FAILED, reason="a fault record already exists; clear it first")
    before = pooler(kubectl)
    if before["replicas"] < 1 or before["ready"] != before["replicas"]:
        return report("inject", "unchanged", FAILED, reason="pooler is not fully up", pooler=before)
    write_once(record, {"pooler": before, "injected_at": datetime.now(UTC).isoformat()})
    kubectl("scale", "deployment", POOLER, f"--current-replicas={before['replicas']}", "--replicas=0")
    kubectl("wait", "--for=delete", "pod", "-l", f"app={POOLER}", f"--timeout={wait}s", timeout=wait)
    after = pooler(kubectl)
    if after["uid"] != before["uid"] or after["replicas"] != 0 or after["ready"]:
        return report("inject", "unknown", UNKNOWN, reason="pooler did not reach 0", pooler=after)
    return report("inject", "injected", PASSED, pooler=after, restore_replicas=before["replicas"])


def clear(kubectl, record, wait):
    if not record.exists():
        return report("clear", "unchanged", FAILED, reason="no fault record")
    saved = json.loads(record.read_text())["pooler"]
    now = pooler(kubectl)
    if now["uid"] != saved["uid"]:
        return report("clear", "unchanged", FAILED, reason="pooler Deployment was replaced; restore by hand")
    if now["replicas"] == 0:
        kubectl("scale", "deployment", POOLER, "--current-replicas=0", f"--replicas={saved['replicas']}")
    kubectl("rollout", "status", f"deployment/{POOLER}", f"--timeout={wait}s", timeout=wait)
    after = pooler(kubectl)
    if after["replicas"] != saved["replicas"] or after["ready"] != saved["replicas"]:
        return report("clear", "unknown", UNKNOWN, reason="pooler not ready", pooler=after)
    record.rename(record.with_suffix(".cleared.json"))
    return report("clear", "cleared", PASSED, pooler=after)


def main(argv=None, kubectl=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["status", "inject", "clear"])
    p.add_argument("--record", type=Path, default=Path("demo-fault.json"),
                   help="state file kept by the operator between inject and clear")
    p.add_argument("--kubeconfig")
    p.add_argument("--wait", type=int, default=120)
    a = p.parse_args(argv)
    if a.kubeconfig and not os.path.isfile(a.kubeconfig):
        return report(a.action, "unchanged", INPUT_ERROR, reason="kubeconfig file not found")
    k = kubectl or kubectl_with(a.kubeconfig)
    try:
        if a.action == "status":
            return report("status", "injected" if a.record.exists() else "clear", PASSED, pooler=pooler(k))
        return (inject if a.action == "inject" else clear)(k, a.record, a.wait)
    except (Unknown, KeyError, ValueError) as problem:
        return report(a.action, "unknown", UNKNOWN, reason=str(problem))


if __name__ == "__main__":
    sys.exit(main())
