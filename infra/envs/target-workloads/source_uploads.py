#!/usr/bin/env python3
"""Source demo-chat switches for the happy path: open writes at S0, stop uploads before the T8 copy.

`open-writes` (S0, T8 owner): the source must run normally until the move, but a new source Pod
starts with WRITE_BLOCKED=true. It opens every source Pod only when the target write switch reads
`closed` and the target app role cannot log in (write_switch.py status), so at most one DB takes
business writes. A Pod that does not end open closes every Pod again. Closing for the cutover is the
Executor's job (write_pause_budget_ms); this tool never closes writes on its own.

`stop` / `restore`:

Happy-path step s3-attachment-copy begins with "업로드 중지": the copy Job cannot see uploads that
arrive after it starts. The source image (demo-chat 8d82a92) reads ATTACHMENT_UPLOAD_ENABLED only
at start, so changing it rolls the Pod. A new source Pod starts with WRITE_BLOCKED=true
(source-workloads render.py), so a plain env change would also stop every chat write. This tool
reads the write switch first, changes the env, waits for the rollout and then puts the switch back
to the value it read: open stays open, blocked stays blocked. It never opens a blocked source.

The switch is called inside each Pod with that Pod's own ADMIN_TOKEN, as write_switch.py does. The
token never appears in arguments, output or files. Every action but `status` appends one JSON line
to --record (times and Pod names for the run log) and prints the same line.
Exit codes: 0 done, 1 a state was wrong, 2 input error, 3 a state could not be confirmed.
"""
import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

PASSED, FAILED, INPUT_ERROR, UNKNOWN = 0, 1, 2, 3
NAMESPACE, DEPLOYMENT, SELECTOR, CONTAINER = "source-dev", "demo-chat", "app=demo-chat", "app"
SETTING = "ATTACHMENT_UPLOAD_ENABLED"

# Runs inside an app Pod. It prints the HTTP status and the two switches, never the token or a body.
IN_POD = r"""
import json, os, sys, urllib.error, urllib.request
action = sys.argv[1]

def call(path, method="GET", body=None, admin=False):
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = "Bearer " + os.environ["ADMIN_TOKEN"]
    request = urllib.request.Request("http://127.0.0.1:8080" + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        return error.code, b""

if action == "info":
    status, raw = call("/api/info")
    info = json.loads(raw) if status == 200 else {}
    print(json.dumps({"status": status, "write_blocked": info.get("write_blocked"),
                      "attachment_upload_enabled": info.get("attachment_upload_enabled")}))
else:
    status, raw = call("/admin/write-block", "PUT", json.dumps({"blocked": action == "close"}).encode(), admin=True)
    blocked = json.loads(raw).get("write_blocked") if status == 200 else None
    print(json.dumps({"status": status, "write_blocked": blocked}))
"""


class Unknown(Exception):
    """A state could not be read or confirmed."""


class Failed(Exception):
    """A state was read and it is the wrong one."""


def now():
    return datetime.now(UTC).isoformat()


def kubectl_with(kubeconfig):
    def kubectl(args):
        command = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), *args]
        timeout = 240 if args[:2] == ["rollout", "status"] else 40
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown(f"kubectl {' '.join(args[:3])} could not run") from None
        if result.returncode:
            raise Unknown(f"kubectl {' '.join(args[:3])} failed")
        return result.stdout
    return kubectl


def running_pods(kubectl):
    """Names of Ready app Pods that are not shutting down."""
    try:
        items = json.loads(kubectl(["get", "pods", "-n", NAMESPACE, "-l", SELECTOR, "-o", "json"]))["items"]
    except (ValueError, KeyError, TypeError):
        raise Unknown("app Pods could not be listed") from None
    names = []
    for item in items:
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in item.get("status", {}).get("conditions", []))
        if ready and not item["metadata"].get("deletionTimestamp"):
            names.append(item["metadata"]["name"])
    if not names:
        raise Unknown("no Ready app Pod")
    return sorted(names)


def in_pod(kubectl, name, action):
    raw = kubectl(["exec", name, "-n", NAMESPACE, "-c", CONTAINER, "--", "python", "-c", IN_POD, action])
    try:
        return json.loads(raw.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Unknown(f"{name} answered nothing readable") from None


def read_state(kubectl):
    """One agreed {write_blocked, attachment_upload_enabled} across all Ready Pods, plus their names."""
    pods = running_pods(kubectl)
    states = {}
    for name in pods:
        info = in_pod(kubectl, name, "info")
        if info.get("status") != 200 or not isinstance(info.get("write_blocked"), bool) \
                or not isinstance(info.get("attachment_upload_enabled"), bool):
            raise Unknown(f"{name} /api/info could not be read")
        states[name] = {"write_blocked": info["write_blocked"],
                        "attachment_upload_enabled": info["attachment_upload_enabled"]}
    return pods, states


def agreed(states):
    values = {json.dumps(v, sort_keys=True) for v in states.values()}
    if len(values) != 1:
        raise Unknown("app Pods disagree on the switches; nothing was changed")
    return json.loads(values.pop())


def change(kubectl, enabled, result):
    """Set uploads to `enabled`, then put the write switch back to what it was. Fills `result`."""
    result["requested_at"] = now()
    pods_before, states = read_state(kubectl)
    before = agreed(states)
    result.update(before=before, pods_before=pods_before)
    if before["attachment_upload_enabled"] is enabled:
        result.update(unchanged=True, after=before, pods_after=pods_before, rolled_out_at=None,
                      writes_restored_at=None)
        return
    kubectl(["set", "env", f"deployment/{DEPLOYMENT}", "-n", NAMESPACE, "-c", CONTAINER,
             f"{SETTING}={'true' if enabled else 'false'}"])
    kubectl(["rollout", "status", f"deployment/{DEPLOYMENT}", "-n", NAMESPACE, "--timeout=180s"])
    result["rolled_out_at"] = now()
    pods_after = running_pods(kubectl)
    result.update(pods_after=pods_after, writes_restored_at=None)
    if not before["write_blocked"]:
        for name in pods_after:
            if in_pod(kubectl, name, "open").get("status") != 200:
                raise Failed(f"{name} refused the write reopen; source writes are left blocked")
        result["writes_restored_at"] = now()
    _, states = read_state(kubectl)
    after = agreed(states)
    result["after"] = after
    if after["attachment_upload_enabled"] is not enabled:
        raise Failed(f"new Pods still report upload enabled={after['attachment_upload_enabled']}")
    if after["write_blocked"] is not before["write_blocked"]:
        raise Failed("source write switch did not return to its earlier value "
                     f"(before blocked={before['write_blocked']}, now blocked={after['write_blocked']})")


def target_status_with(python=sys.executable):
    """write_switch.py status on the target, as the dict it prints. Read-only."""
    def read(kubeconfig):
        command = [python, str(HERE / "write_switch.py"), "status", "--kubeconfig", kubeconfig]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            raise Unknown("target write switch could not be read") from None
    return read


def open_writes(kubectl, target_status, target_kubeconfig, result):
    """Open every source Pod, only while the target cannot take writes. Fills `result`."""
    result["requested_at"] = now()
    target = target_status(target_kubeconfig)
    result["target_state"] = target.get("state")
    if target.get("state") != "closed" or target.get("checks", {}).get("target_app_role") != "nologin":
        raise Failed(f"target is not closed (state={target.get('state')}, "
                     f"app role={target.get('checks', {}).get('target_app_role')}); source stays as it is")
    pods, states = read_state(kubectl)
    before = agreed(states)
    result.update(before=before, pods_before=pods)
    if not before["write_blocked"]:
        result.update(unchanged=True, after=before, writes_opened_at=None)
        return
    refused = [name for name in pods if in_pod(kubectl, name, "open").get("status") != 200]
    if refused:
        for name in pods:
            in_pod(kubectl, name, "close")
        raise Failed(f"{', '.join(refused)} refused to open; every source Pod was closed again")
    result["writes_opened_at"] = now()
    _, states = read_state(kubectl)
    result["after"] = agreed(states)
    if result["after"]["write_blocked"]:
        for name in pods:
            in_pod(kubectl, name, "close")
        raise Failed("source did not read back open; every source Pod was closed again")


def main(argv=None, kubectl=None, target_status=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["status", "open-writes", "stop", "restore"])
    parser.add_argument("--kubeconfig", help="source EKS kubeconfig")
    parser.add_argument("--target-kubeconfig", help="target RKE2 kubeconfig (required for open-writes)")
    parser.add_argument("--record", type=Path, help="append-only run record (required except for status)")
    args = parser.parse_args(argv)
    if args.action != "status" and not args.record:
        print(json.dumps({"error": "--record is required for open-writes, stop and restore"}))
        return INPUT_ERROR
    if args.action == "open-writes" and not args.target_kubeconfig:
        print(json.dumps({"error": "--target-kubeconfig is required for open-writes"}))
        return INPUT_ERROR
    kubectl = kubectl or kubectl_with(args.kubeconfig)
    result = {"action": args.action}
    code = PASSED
    try:
        if args.action == "status":
            _, states = read_state(kubectl)
            result["pods"] = states
        elif args.action == "open-writes":
            open_writes(kubectl, target_status or target_status_with(), args.target_kubeconfig, result)
        else:
            change(kubectl, args.action == "restore", result)
    except Failed as error:
        code, result["error"] = FAILED, str(error)
    except Unknown as error:
        code, result["error"] = UNKNOWN, str(error)
    if code == FAILED and "after" not in result:
        try:
            result["after"] = agreed(read_state(kubectl)[1])
        except Unknown:
            pass
    if args.action != "status":
        result["exit_code"] = code
        with args.record.open("a") as record:
            record.write(json.dumps(result, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
