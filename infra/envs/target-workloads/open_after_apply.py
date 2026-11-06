#!/usr/bin/env python3
"""After a real T31 APPLIED rollout, open the target demo-chat writes once with write_switch.py.

Happy-path step "T31 rollout -> write_switch.py open". The operator starts it; it adds no check
of its own. It reads the recorded T31 result with `bravo-apply get-result` (no cluster call),
waits while `write_switch.py status` cannot yet confirm the state (rollout still settling), then
runs `open` once. write_switch.py decides and, on failure, closes every Pod again.

One record per application ID is kept next to the T31 receipts. A marker is written before
`open`, so a crash or rerun never opens twice; read the switch with `write_switch.py status`.
Exit codes: 0 opened, 1 a state was wrong or T31 did not apply, 2 input error, 3 not confirmed.
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import write_switch
from render import NAMESPACE

PASSED, FAILED, INPUT_ERROR, UNKNOWN = 0, 1, 2, 3
ROOT = Path(__file__).resolve().parents[3]
APPLY_CLI = ["uv", "run", "--quiet", "--project", str(ROOT / "services/apply-path"), "bravo-apply"]


def now():
    return datetime.now(UTC).isoformat()


def t31_result(application_id, receipts, run=subprocess.run):
    """The recorded T31 outcome, or None when it is not a real applied image rollout here."""
    out = run([*APPLY_CLI, "get-result", "--application-id", application_id, "--receipts", str(receipts)],
              capture_output=True, text=True, timeout=120, check=False)
    try:
        result = json.loads(out.stdout.splitlines()[0])
    except (IndexError, ValueError):
        return None
    target = result.get("target") or {}
    if (result.get("outcome") == "APPLIED" and result.get("job_passed") is True
            and result.get("cluster") == "kubectl" and result.get("deployed_digest")
            and target.get("namespace") == NAMESPACE and target.get("deployment") == write_switch.DEPLOYMENT):
        return result
    return None


def switch(action, kubeconfig):
    """Run write_switch.py in-process; return its exit code and its one JSON line."""
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        code = write_switch.main([action, *(["--kubeconfig", kubeconfig] if kubeconfig else [])])
    lines = stream.getvalue().splitlines()
    return code, json.loads(lines[-1]) if lines else None


def write_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None, *, run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--application-id", required=True, help="the T31 run's application_id")
    p.add_argument("--receipts", required=True, type=Path, help="the T31 --receipts directory")
    p.add_argument("--kubeconfig", help="target cluster; default: kubectl's own")
    p.add_argument("--wait", type=int, default=300, help="seconds to wait for status to confirm")
    p.add_argument("--interval", type=int, default=10)
    a = p.parse_args(argv)
    if not a.receipts.is_dir() or "/" in a.application_id or (a.kubeconfig and not os.path.isfile(a.kubeconfig)):
        print(json.dumps({"state": "unchanged", "reason": "input error"}))
        return INPUT_ERROR
    marker = a.receipts / f"{a.application_id}.write-switch.started.json"
    record = a.receipts / f"{a.application_id}.write-switch.json"
    if record.exists() or marker.exists():
        # ponytail: never a second open per T31 run; a retry is a human running write_switch.py open.
        print(record.read_text() if record.exists() else json.dumps(
            {"state": "unknown", "reason": "an earlier open did not finish; read write_switch.py status"}))
        return json.loads(record.read_text())["exit_code"] if record.exists() else UNKNOWN
    applied = t31_result(a.application_id, a.receipts, run)
    if applied is None:
        print(json.dumps({"state": "unchanged", "reason": "no real T31 APPLIED rollout of the target app"}))
        return FAILED
    deadline, polls = clock() + a.wait, 0
    while True:
        polls += 1
        code, status = switch("status", a.kubeconfig)
        if code != UNKNOWN or clock() >= deadline:
            break
        sleep(a.interval)
    base = {"application_id": a.application_id, "deployed_digest": applied["deployed_digest"],
            "plan_id": applied.get("plan_id"), "status_polls": polls, "status": status}
    if code != PASSED:
        value = base | {"exit_code": code, "opened": False, "observed_at": now()}
        write_once(record, value)
        print(json.dumps(value, sort_keys=True))
        return code
    write_once(marker, base | {"started_at": now()})
    code, opened = switch("open", a.kubeconfig)
    value = base | {"exit_code": code, "opened": code == PASSED, "open": opened, "observed_at": now()}
    write_once(record, value)
    print(json.dumps(value, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
