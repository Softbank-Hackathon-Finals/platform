#!/usr/bin/env python3
"""Read, open or close the target demo-chat write switch.

`open` changes nothing until every check passes: the rollout is finished, every app Pod is Ready
and none is retiring, `/api/health/db` is 200 in each Pod, the pooler points at the source DB and
the target app role cannot log in. It then opens each Pod, reads every Pod back and closes all of
them again if any Pod does not end open. `close` needs no check and reaches every running Pod.

The switch is called inside each Pod with that Pod's own ADMIN_TOKEN, as the cutover executor does.
The token never appears in arguments, output or files. One JSON line goes to stdout.
Exit codes: 0 passed, 1 a check read a wrong state, 2 input error, 3 a state could not be confirmed.
"""
import argparse
import json
import os
import re
import subprocess
import sys

from pooler import SOURCE_HOST
from render import NAMESPACE

PASSED, FAILED, INPUT_ERROR, UNKNOWN = 0, 1, 2, 3
DEPLOYMENT, APP_SELECTOR, APP_CONTAINER = "demo-chat", "app=demo-chat", "app"
POOLER_SELECTOR, POOLER_CONTAINER = "app=target-pgbouncer", "target-pgbouncer"
POOLER_FILE, POOLER_CONFIGMAP = "/etc/pgbouncer/pgbouncer.ini", "target-pgbouncer-config"
PRIMARY_SELECTOR = "cnpg.io/cluster=target-db,cnpg.io/instanceRole=primary"
TARGET_HOST = f"target-db-rw.{NAMESPACE}.svc.cluster.local"
ROLE_QUERY = "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'"

# Runs inside an app Pod. It prints the HTTP status and the switch state, never the token or a body.
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

if action == "health":
    print(json.dumps({"health_db": call("/api/health/db")[0]}))
else:
    body = None if action == "get" else json.dumps({"blocked": action == "close"}).encode()
    status, raw = call("/admin/write-block", "GET" if action == "get" else "PUT", body, admin=True)
    blocked = json.loads(raw).get("write_blocked") if status == 200 else None
    print(json.dumps({"status": status, "write_blocked": blocked}))
"""


class Unknown(Exception):
    """A state could not be read or confirmed."""


class Failed(Exception):
    """A state was read and it is the wrong one."""


def kubectl_with(kubeconfig):
    def kubectl(args):
        command = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown(f"kubectl {' '.join(args[2:5])} could not run") from None
        if result.returncode:
            # stderr is not echoed: it can quote a manifest. The verb and target are enough to retry.
            raise Unknown(f"kubectl {' '.join(args[2:5])} failed")
        return result.stdout
    return kubectl


def database_host(ini):
    """host= of the chat entry in [databases], or None."""
    section = ""
    for line in ini.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        elif section == "[databases]" and re.match(r"chat\s*=", stripped):
            found = re.search(r"\bhost=(\S+)", stripped)
            return found.group(1) if found else None
    return None


class Switch:
    def __init__(self, kubectl):
        self._kubectl = kubectl
        self.checks = {}

    def _call(self, *args):
        return self._kubectl(["-n", NAMESPACE, *args])

    def _json(self, what, *args):
        try:
            return json.loads(self._call("get", *args, "-o", "json"))
        except ValueError:
            raise Unknown(f"{what} could not be read") from None

    def pods(self, selector):
        try:
            items = self._json(f"pods {selector}", "pods", "-l", selector)["items"]
            pods = []
            for item in items:
                conditions = {c["type"]: c["status"] for c in item["status"].get("conditions") or []}
                pods.append({"name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
                             "running": item["status"].get("phase") == "Running",
                             "ready": item["status"].get("phase") == "Running" and conditions.get("Ready") == "True",
                             "terminating": bool(item["metadata"].get("deletionTimestamp"))})
        except Unknown:
            raise Unknown(f"pods {selector} could not be read") from None
        except (KeyError, TypeError, AttributeError):
            raise Unknown(f"pods {selector} could not be read") from None
        return sorted(pods, key=lambda p: p["name"])

    def _in_app(self, pod, action):
        try:
            return json.loads(self._call("exec", pod, "-c", APP_CONTAINER, "--", "python", "-c", IN_POD, action))
        except (Unknown, ValueError):
            raise Unknown(f"{pod}: {action} could not be confirmed") from None

    def set(self, pod, action):
        """Call the switch in one Pod and return the state it answers with."""
        answer = self._in_app(pod, action)
        if answer.get("status") != 200 or type(answer.get("write_blocked")) is not bool:
            raise Unknown(f"{pod}: write switch answered {answer.get('status')}")
        return answer["write_blocked"]

    def settled_pods(self):
        try:
            deployment = self._json(f"deployment {DEPLOYMENT}", "deployment", DEPLOYMENT)
            wanted, status = deployment["spec"]["replicas"], deployment["status"]
            settled = (deployment["metadata"]["generation"] <= status.get("observedGeneration", 0)
                       and all(status.get(k, 0) == wanted for k in ("replicas", "updatedReplicas",
                                                                    "readyReplicas", "availableReplicas")))
        except Unknown:
            raise Unknown(f"deployment {DEPLOYMENT} could not be read") from None
        except (KeyError, TypeError):
            raise Unknown(f"deployment {DEPLOYMENT} could not be read") from None
        if not settled:
            raise Unknown("deployment rollout is not finished")
        pods = self.pods(APP_SELECTOR)
        if any(p["terminating"] for p in pods):
            # During preStop a retiring Pod still answers with the switch state it had.
            raise Unknown("a retiring app Pod is still terminating")
        if not pods:
            raise Unknown("no app Pod")
        if len(pods) != wanted:
            raise Unknown("app Pod count differs from the deployment rollout")
        for pod in pods:
            if not pod["ready"]:
                raise Unknown(f"app Pod {pod['name']} is not Ready")
        self.checks["rollout"] = "settled"
        return pods

    def pooler_target(self):
        pods = self.pods(POOLER_SELECTOR)
        if len(pods) != 1 or not pods[0]["ready"] or pods[0]["terminating"]:
            raise Unknown("expected exactly one Ready pooler Pod")
        try:
            live = self._call("exec", pods[0]["name"], "-c", POOLER_CONTAINER, "--", "cat", POOLER_FILE)
            stored = self._json("pooler ConfigMap", "configmap", POOLER_CONFIGMAP)["data"]["pgbouncer.ini"]
        except (Unknown, KeyError, TypeError):
            raise Unknown("pooler configuration could not be read") from None
        host, stored_host = database_host(live), database_host(stored)
        if host != stored_host:
            # The executor rewrites the file and then the ConfigMap; between the two nothing is settled.
            raise Unknown("pooler file and ConfigMap differ: a change is in progress")
        if host == SOURCE_HOST:
            self.checks["pooler_target"] = "source"
            return
        if host == TARGET_HOST:
            self.checks["pooler_target"] = "target"
            raise Failed("pooler points at the target DB")
        self.checks["pooler_target"] = "unknown"
        raise Unknown("pooler has no chat entry for a registered host")

    def target_role(self):
        primaries = [p for p in self.pods(PRIMARY_SELECTOR) if not p["terminating"]]
        if len(primaries) != 1:
            raise Unknown("expected exactly one target DB primary")
        try:
            answer = self._call("exec", primaries[0]["name"], "-c", "postgres", "--",
                                "psql", "-X", "-qtA", "-d", "chat", "-c", ROLE_QUERY).strip()
        except Unknown:
            answer = ""
        if answer == "f":
            self.checks["target_app_role"] = "nologin"
            return
        if answer == "t":
            self.checks["target_app_role"] = "login"
            raise Failed("target app role can LOGIN: the target DB could take business writes")
        raise Unknown("target app role could not be read")

    def inspect(self):
        """Every read `open` depends on. Raises at the first state that is wrong or unreadable."""
        pods = self.settled_pods()
        for pod in pods:
            pod["write_blocked"] = self.set(pod["name"], "get")
            pod["health_db"] = self._in_app(pod["name"], "health").get("health_db")
        bad = [p for p in pods if p["health_db"] != 200]
        if bad:
            self.checks["database_path"] = "closed"
            raise Failed(f"/api/health/db is {bad[0]['health_db']} on {bad[0]['name']}")
        self.checks["database_path"] = "open"
        self.pooler_target()
        self.target_role()
        return pods

    def apply(self, pods, action):
        """Call every Pod and read every Pod back. Returns {name: result} and whether all ended as wanted."""
        wanted = action == "close"
        results = {}
        for pod in pods:
            try:
                results[pod["name"]] = action + "ed" if self.set(pod["name"], action) == wanted else "unchanged"
            except Unknown:
                results[pod["name"]] = "unreachable"
        read = {}
        for pod in pods:
            try:
                read[pod["name"]] = self.set(pod["name"], "get")
            except Unknown:
                read[pod["name"]] = None
        return results, read


def state_of(values):
    values = list(values)
    if not values or None in values:
        return "unknown"
    if all(v is True for v in values):
        return "closed"
    return "open" if all(v is False for v in values) else "mixed"


def report(action, switch, pods, *, state, reason="", **extra):
    print(json.dumps({"action": action, "state": state, "reason": reason, "checks": switch.checks,
                      "pods": pods, **extra}, sort_keys=True))


def do_status(switch):
    try:
        pods = switch.inspect()
    except (Failed, Unknown) as problem:
        report("status", switch, [], state="unknown", reason=str(problem))
        return FAILED if isinstance(problem, Failed) else UNKNOWN
    state = state_of(p["write_blocked"] for p in pods)
    report("status", switch, pods, state=state, reason="pods disagree" if state == "mixed" else "")
    return FAILED if state == "mixed" else PASSED


def do_open(switch):
    try:
        before = switch.inspect()
    except (Failed, Unknown) as problem:
        report("open", switch, [], state="unchanged", reason=str(problem))
        return FAILED if isinstance(problem, Failed) else UNKNOWN
    results, read = switch.apply(before, "open")
    problem, code = "", PASSED
    try:
        now = switch.pods(APP_SELECTOR)
    except Unknown:
        now = None
    if now is None or [(p["name"], p["uid"]) for p in now] != [(p["name"], p["uid"]) for p in before]:
        problem, code = "app Pod set changed during open", UNKNOWN
    elif any(r == "unreachable" for r in results.values()) or None in read.values():
        problem, code = "a Pod could not be opened or read back", FAILED
    elif any(r != "opened" for r in results.values()) or any(v is not False for v in read.values()):
        problem, code = "a Pod did not end open", FAILED
    if not problem:
        pods = [{**p, "result": results[p["name"]], "write_blocked": read[p["name"]]} for p in before]
        report("open", switch, pods, state="open", rolled_back=False)
        return PASSED
    # Fail closed: a partly open entry is not a state anyone asked for.
    targets = {p["name"]: p for p in [*before, *(now or [])]}.values()
    _, closed = switch.apply([p for p in targets if p.get("running", True)], "close")
    state = state_of(closed.values())
    pods = [{**p, "result": results.get(p["name"], "not_called"), "write_blocked": closed.get(p["name"])}
            for p in targets]
    report("open", switch, pods, state=state, reason=problem, rolled_back=state == "closed")
    return code if state == "closed" else UNKNOWN


def do_close(switch):
    try:
        listed = switch.pods(APP_SELECTOR)
    except Unknown as problem:
        report("close", switch, [], state="unknown", reason=str(problem))
        return UNKNOWN
    running = [p for p in listed if p["running"]]
    if not running:
        report("close", switch, listed, state="unknown", reason="no running app Pod to close")
        return UNKNOWN
    results, read = switch.apply(running, "close")
    pods = [{**p, "result": results.get(p["name"], "not_running"), "write_blocked": read.get(p["name"])} for p in listed]
    state = state_of(read.values())
    try:
        changed = [p["uid"] for p in switch.pods(APP_SELECTOR) if p["running"]] != [p["uid"] for p in running]
    except Unknown:
        changed = True
    if changed or "unreachable" in results.values() or state == "unknown":
        report("close", switch, pods, state="unknown", reason="a Pod could not be closed or confirmed")
        return UNKNOWN
    if state != "closed":
        report("close", switch, pods, state=state, reason="a Pod did not end closed")
        return FAILED
    report("close", switch, pods, state="closed")
    return PASSED


def main(argv=None, kubectl=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["status", "open", "close"])
    parser.add_argument("--kubeconfig", help="default: kubectl's own (KUBECONFIG or ~/.kube/config)")
    args = parser.parse_args(argv)
    if args.kubeconfig and not os.path.isfile(args.kubeconfig):
        print(json.dumps({"action": args.action, "state": "unchanged", "reason": "kubeconfig file not found"}))
        return INPUT_ERROR
    switch = Switch(kubectl or kubectl_with(args.kubeconfig))
    return {"status": do_status, "open": do_open, "close": do_close}[args.action](switch)


if __name__ == "__main__":
    sys.exit(main())
