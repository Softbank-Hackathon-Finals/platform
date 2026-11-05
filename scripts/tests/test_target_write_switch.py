"""Target demo-chat write switch: open only on a proven pre-cutover route, and never leak the token."""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "infra/envs/target-workloads"
CANARY = "t30-admin-token-canary"


def load():
    with patch.dict(sys.modules), patch.object(sys, "path", [str(WORKLOADS), *sys.path]):
        sys.modules.pop("render", None)
        spec = importlib.util.spec_from_file_location("target_write_switch", WORKLOADS / "write_switch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


switch = load()
SOURCE_HOST = "source-db-rw.source-dev.svc.cluster.local"
TARGET_HOST = "target-db-rw.target-dev.svc.cluster.local"


def ini(host):
    return f"[databases]\nchat = host={host} port=5432 dbname=chat\n\n[pgbouncer]\nlisten_port = 5432\n"


def pod(name, uid, *, ready=True, terminating=False, phase="Running"):
    metadata = {"name": name, "uid": uid, "namespace": "target-dev"}
    if terminating:
        metadata["deletionTimestamp"] = "2026-11-06T00:00:00Z"
    return {"kind": "Pod", "metadata": metadata,
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class FakeCluster:
    """Answers exactly the kubectl calls the switch makes. Holds the admin token as a Pod would."""

    def __init__(self):
        self.deployment = {"metadata": {"name": "demo-chat", "generation": 3},
                           "spec": {"replicas": 2},
                           "status": {"observedGeneration": 3, "replicas": 2, "updatedReplicas": 2,
                                      "readyReplicas": 2, "availableReplicas": 2}}
        self.apps = [pod("demo-chat-a", "uid-a"), pod("demo-chat-b", "uid-b")]
        self.blocked = {"demo-chat-a": True, "demo-chat-b": True}
        self.health = {"demo-chat-a": 200, "demo-chat-b": 200}
        self.poolers = [pod("target-pgbouncer-1", "uid-p")]
        self.pooler_file = ini(SOURCE_HOST)
        self.pooler_configmap = ini(SOURCE_HOST)
        self.databases = [pod("target-db-1", "uid-d")]
        self.can_login = "f"
        self.broken = set()        # (verb, target) pairs that fail like an unreachable call
        self.ignore = set()        # pods that answer but keep their old switch state
        self.after_open = None     # a callable that changes the cluster after the first open call
        self.calls = []
        self.token = CANARY        # stays here, as it would stay in the Pod

    def _fail(self, *key):
        if key in self.broken:
            raise switch.Unknown("simulated unreachable call")

    def __call__(self, args):
        self.calls.append(list(args))
        assert args[:2] == ["-n", "target-dev"], args
        verb = args[2]
        if verb == "get":
            kind, name = args[3], args[4]
            self._fail("get", name if kind != "pods" else args[5])
            if kind == "deployment":
                return json.dumps(self.deployment)
            if kind == "configmap":
                return json.dumps({"data": {"pgbouncer.ini": self.pooler_configmap}})
            selector = args[5]
            items = {"app=demo-chat": self.apps, "app=target-pgbouncer": self.poolers,
                     "cnpg.io/cluster=target-db,cnpg.io/instanceRole=primary": self.databases}[selector]
            return json.dumps({"kind": "List", "items": items})
        assert verb == "exec", args
        target, container, command = args[3], args[5], args[7:]
        assert args[4] == "-c" and args[6] == "--", args
        if container == "app":
            action = command[-1]
            self._fail(action, target)
            assert command[:2] == ["python", "-c"] and command[2] == switch.IN_POD, command
            if action == "health":
                return json.dumps({"health_db": self.health[target]})
            if action in {"open", "close"} and target not in self.ignore:
                self.blocked[target] = action == "close"
                if action == "open" and self.after_open:
                    change, self.after_open = self.after_open, None
                    change(self)
            return json.dumps({"status": 200, "write_blocked": self.blocked[target]})
        if container == "target-pgbouncer":
            self._fail("cat", target)
            assert command == ["cat", "/etc/pgbouncer/pgbouncer.ini"], command
            return self.pooler_file
        assert container == "postgres", args
        self._fail("psql", target)
        assert command[:2] == ["psql", "-X"], command
        return self.can_login + "\n"

    def actions(self, action):
        return [c[3] for c in self.calls if c[2] == "exec" and c[-1] == action]


def run(cluster, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = switch.main(list(argv), kubectl=cluster)
    return code, json.loads(out.getvalue())


class StatusTests(unittest.TestCase):
    def test_status_reads_every_check_and_changes_nothing(self):
        cluster = FakeCluster()
        code, report = run(cluster, "status")
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "closed")
        self.assertEqual([p["name"] for p in report["pods"]], ["demo-chat-a", "demo-chat-b"])
        self.assertTrue(all(p["write_blocked"] is True and p["health_db"] == 200 for p in report["pods"]))
        self.assertEqual(report["checks"]["pooler_target"], "source")
        self.assertEqual(report["checks"]["target_app_role"], "nologin")
        self.assertEqual(cluster.actions("open") + cluster.actions("close"), [])

    def test_status_reports_a_mixed_switch_as_a_failure(self):
        cluster = FakeCluster()
        cluster.blocked["demo-chat-b"] = False
        code, report = run(cluster, "status")
        self.assertEqual((code, report["state"]), (1, "mixed"))

    def test_status_says_open_when_every_pod_is_open(self):
        cluster = FakeCluster()
        cluster.blocked = {name: False for name in cluster.blocked}
        code, report = run(cluster, "status")
        self.assertEqual((code, report["state"]), (0, "open"))


class OpenTests(unittest.TestCase):
    def test_open_opens_every_pod_and_reads_it_back(self):
        cluster = FakeCluster()
        code, report = run(cluster, "open")
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "open")
        self.assertEqual(cluster.blocked, {"demo-chat-a": False, "demo-chat-b": False})
        self.assertEqual(sorted(cluster.actions("open")), ["demo-chat-a", "demo-chat-b"])
        self.assertEqual([p["write_blocked"] for p in report["pods"]], [False, False])
        # Read back after every open, not only from the PUT responses.
        last_open = max(i for i, c in enumerate(cluster.calls) if c[-1] == "open")
        reads = [c[3] for c in cluster.calls[last_open + 1:] if c[-1] == "get"]
        self.assertEqual(sorted(reads), ["demo-chat-a", "demo-chat-b"])

    def check_refused(self, change, code, reason):
        cluster = FakeCluster()
        change(cluster)
        got, report = run(cluster, "open")
        self.assertEqual(got, code, report)
        self.assertEqual(cluster.actions("open"), [], "nothing may open before every check passes")
        self.assertIn(reason, report["reason"])
        return report

    def test_unready_pod_is_unknown(self):
        self.check_refused(lambda c: c.apps.__setitem__(1, pod("demo-chat-b", "uid-b", ready=False)), 3, "Ready")

    def test_retiring_pod_is_unknown(self):
        self.check_refused(lambda c: c.apps.append(pod("demo-chat-old", "uid-o", terminating=True)), 3, "terminating")

    def test_unfinished_rollout_is_unknown(self):
        self.check_refused(lambda c: c.deployment["status"].update(updatedReplicas=1), 3, "rollout")

    def test_stale_controller_view_is_unknown(self):
        self.check_refused(lambda c: c.deployment["status"].update(observedGeneration=2), 3, "rollout")

    def test_pod_count_that_differs_from_the_deployment_is_unknown(self):
        self.check_refused(lambda c: c.apps.pop(), 3, "rollout")

    def test_no_app_pod_is_unknown(self):
        def empty(c):
            c.apps.clear()
            c.deployment["spec"]["replicas"] = 0
            c.deployment["status"].update(replicas=0, updatedReplicas=0, readyReplicas=0, availableReplicas=0)
        self.check_refused(empty, 3, "no app Pod")

    def test_database_path_not_open_is_a_failure(self):
        self.check_refused(lambda c: c.health.__setitem__("demo-chat-b", 503), 1, "/api/health/db")

    def test_unreadable_database_path_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("health", "demo-chat-a")), 3, "demo-chat-a")

    def test_pooler_pointing_at_the_target_db_is_a_failure(self):
        def to_target(c):
            c.pooler_file = c.pooler_configmap = ini(TARGET_HOST)
        report = self.check_refused(to_target, 1, "target DB")
        self.assertEqual(report["checks"]["pooler_target"], "target")

    def test_pooler_pointing_elsewhere_is_unknown(self):
        def elsewhere(c):
            c.pooler_file = c.pooler_configmap = ini("somewhere.example.invalid")
        self.check_refused(elsewhere, 3, "pooler")

    def test_pooler_file_and_configmap_that_differ_are_unknown(self):
        self.check_refused(lambda c: setattr(c, "pooler_configmap", ini(TARGET_HOST)), 3, "differ")

    def test_pooler_without_a_chat_entry_is_unknown(self):
        def missing(c):
            c.pooler_file = c.pooler_configmap = "[databases]\n\n[pgbouncer]\n"
        self.check_refused(missing, 3, "pooler")

    def test_pooler_pod_count_other_than_one_is_unknown(self):
        self.check_refused(lambda c: c.poolers.append(pod("target-pgbouncer-2", "uid-q")), 3, "pooler")
        self.check_refused(lambda c: c.poolers.clear(), 3, "pooler")

    def test_unready_or_retiring_pooler_is_unknown(self):
        self.check_refused(lambda c: c.poolers.__setitem__(0, pod("target-pgbouncer-1", "uid-p", ready=False)), 3, "pooler")
        self.check_refused(lambda c: c.poolers.__setitem__(0, pod("target-pgbouncer-1", "uid-p", terminating=True)), 3, "pooler")

    def test_unreadable_pooler_file_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("cat", "target-pgbouncer-1")), 3, "pooler")

    def test_target_app_role_that_can_log_in_is_a_failure(self):
        report = self.check_refused(lambda c: setattr(c, "can_login", "t"), 1, "LOGIN")
        self.assertEqual(report["checks"]["target_app_role"], "login")

    def test_unreadable_target_app_role_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("psql", "target-db-1")), 3, "role")
        self.check_refused(lambda c: setattr(c, "can_login", ""), 3, "role")

    def test_primary_count_other_than_one_is_unknown(self):
        self.check_refused(lambda c: c.databases.clear(), 3, "primary")
        self.check_refused(lambda c: c.databases.append(pod("target-db-2", "uid-e")), 3, "primary")

    def test_unreadable_switch_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("get", "demo-chat-b")), 3, "demo-chat-b")

    def test_unreadable_pod_list_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("get", "app=demo-chat")), 3, "app=demo-chat")

    def test_unreadable_deployment_is_unknown(self):
        self.check_refused(lambda c: c.broken.add(("get", "demo-chat")), 3, "demo-chat")


class PartialFailureTests(unittest.TestCase):
    def test_one_pod_failing_to_open_closes_every_pod_again(self):
        cluster = FakeCluster()
        cluster.broken.add(("open", "demo-chat-b"))
        code, report = run(cluster, "open")
        self.assertEqual(code, 1)
        self.assertEqual(cluster.blocked, {"demo-chat-a": True, "demo-chat-b": True})
        self.assertEqual(report["state"], "closed")
        self.assertTrue(report["rolled_back"])
        by_pod = {p["name"]: p for p in report["pods"]}
        self.assertEqual(by_pod["demo-chat-b"]["result"], "unreachable")
        self.assertEqual(by_pod["demo-chat-a"]["result"], "opened")

    def test_a_pod_that_answers_but_stays_closed_is_rolled_back(self):
        cluster = FakeCluster()
        cluster.ignore.add("demo-chat-b")
        code, report = run(cluster, "open")
        self.assertEqual(code, 1)
        self.assertEqual(cluster.blocked["demo-chat-a"], True)
        self.assertTrue(report["rolled_back"])

    def test_rollback_that_leaves_a_pod_open_is_unknown_and_says_so(self):
        cluster = FakeCluster()
        cluster.broken.update({("open", "demo-chat-b"), ("close", "demo-chat-a")})
        code, report = run(cluster, "open")
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "mixed")
        self.assertFalse(report["rolled_back"])
        self.assertEqual({p["name"]: p["write_blocked"] for p in report["pods"]}, {"demo-chat-a": False, "demo-chat-b": True})

    def test_rollback_that_cannot_read_a_pod_back_is_unknown(self):
        cluster = FakeCluster()
        cluster.broken.add(("open", "demo-chat-b"))
        # Reading works for the checks and stops only after the first Pod opened.
        cluster.after_open = lambda c: c.broken.add(("get", "demo-chat-a"))
        code, report = run(cluster, "open")
        self.assertEqual(code, 3)
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(cluster.actions("close"), ["demo-chat-a", "demo-chat-b"])

    def test_a_pod_set_that_changes_during_open_is_rolled_back_and_unknown(self):
        def replace_pod(c):
            c.apps[1] = pod("demo-chat-c", "uid-c")
            c.blocked["demo-chat-c"] = True
            c.health["demo-chat-c"] = 200
        cluster = FakeCluster()
        cluster.after_open = replace_pod
        code, report = run(cluster, "open")
        self.assertEqual(code, 3)
        self.assertIn("changed", report["reason"])
        self.assertEqual(cluster.blocked["demo-chat-a"], True)

    def test_reading_back_open_but_finding_closed_is_rolled_back(self):
        cluster = FakeCluster()
        original = cluster.__call__

        def stale_reader(args):
            out = original(args)
            if args[-1] == "get" and args[3] == "demo-chat-a" and any(c[-1] == "open" for c in cluster.calls):
                return json.dumps({"status": 200, "write_blocked": True})
            return out

        code, report = run(stale_reader, "open")
        self.assertEqual(code, 1)
        self.assertTrue(report["rolled_back"])


class CloseTests(unittest.TestCase):
    def test_close_needs_no_route_check_and_closes_every_pod(self):
        cluster = FakeCluster()
        cluster.blocked = {name: False for name in cluster.blocked}
        cluster.pooler_file = cluster.pooler_configmap = ini(TARGET_HOST)
        code, report = run(cluster, "close")
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "closed")
        self.assertEqual(cluster.blocked, {"demo-chat-a": True, "demo-chat-b": True})

    def test_close_that_cannot_reach_a_pod_is_unknown(self):
        cluster = FakeCluster()
        cluster.blocked = {name: False for name in cluster.blocked}
        cluster.broken.add(("close", "demo-chat-b"))
        code, report = run(cluster, "close")
        self.assertEqual(code, 3)
        self.assertEqual(cluster.blocked["demo-chat-a"], True)
        self.assertEqual({p["name"]: p["result"] for p in report["pods"]}["demo-chat-b"], "unreachable")

    def test_close_also_reaches_a_pod_that_is_not_ready(self):
        cluster = FakeCluster()
        cluster.blocked = {name: False for name in cluster.blocked}
        cluster.apps[1] = pod("demo-chat-b", "uid-b", ready=False)
        code, _ = run(cluster, "close")
        self.assertEqual(code, 0)
        self.assertEqual(cluster.blocked["demo-chat-b"], True)


class InputTests(unittest.TestCase):
    def test_missing_kubeconfig_is_an_input_error(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(out):
            code = switch.main(["status", "--kubeconfig", str(Path(folder) / "absent")], kubectl=FakeCluster())
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "kubeconfig file not found")

    def test_unknown_action_is_an_input_error(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            switch.main(["toggle"], kubectl=FakeCluster())
        self.assertEqual(raised.exception.code, 2)

    def test_kubeconfig_reaches_kubectl_as_an_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config"
            config.write_text("apiVersion: v1\n")
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": []}), stderr="")
            with patch.object(switch.subprocess, "run", return_value=completed) as called:
                switch.kubectl_with(str(config))(["-n", "target-dev", "get", "pods", "-l", "app=demo-chat", "-o", "json"])
        self.assertEqual(called.call_args.args[0][:3], ["kubectl", "--kubeconfig", str(config)])

    def test_failed_kubectl_is_unknown_without_echoing_its_output(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="forbidden: canary-detail")
        with patch.object(switch.subprocess, "run", return_value=completed), self.assertRaises(switch.Unknown) as raised:
            switch.kubectl_with(None)(["-n", "target-dev", "get", "pods"])
        self.assertNotIn("canary-detail", str(raised.exception))


class TokenTests(unittest.TestCase):
    def test_token_never_reaches_arguments_or_output(self):
        for action in ("status", "open", "close"):
            with self.subTest(action=action):
                cluster = FakeCluster()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    switch.main([action], kubectl=cluster)
                self.assertNotIn(CANARY, json.dumps(cluster.calls))
                self.assertNotIn(CANARY, out.getvalue())
                self.assertNotIn("ADMIN_TOKEN=", json.dumps(cluster.calls))

    def test_in_pod_script_reads_the_token_from_the_pod_and_prints_only_state(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            blocked = True

            def log_message(self, *args):
                pass

            def answer(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                if self.path == "/api/health/db":
                    return self.answer(200, {"ok": True})
                return self.answer(200, {"env": "onprem", "write_blocked": Handler.blocked})

            def do_PUT(self):
                seen.append((self.command, self.path, self.headers.get("Authorization")))
                Handler.blocked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["blocked"]
                return self.answer(200, {"env": "onprem", "write_blocked": Handler.blocked})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = switch.IN_POD.replace("127.0.0.1:8080", f"127.0.0.1:{server.server_address[1]}")
        self.assertNotEqual(script, switch.IN_POD)
        outputs = {}
        for action in ("get", "open", "health", "close"):
            result = subprocess.run([sys.executable, "-c", script, action], capture_output=True, text=True,
                                    env={**os.environ, "ADMIN_TOKEN": CANARY}, timeout=10, check=True)
            self.assertNotIn(CANARY, result.stdout + result.stderr)
            outputs[action] = json.loads(result.stdout)
        self.assertEqual(outputs["open"], {"status": 200, "write_blocked": False})
        self.assertEqual(outputs["close"], {"status": 200, "write_blocked": True})
        self.assertEqual(outputs["health"], {"health_db": 200})
        switch_calls = [s for s in seen if s[1] == "/admin/write-block"]
        self.assertEqual([s[0] for s in switch_calls], ["GET", "PUT", "PUT"])
        self.assertTrue(all(s[2] == "Bearer " + CANARY for s in switch_calls))
        self.assertEqual([s for s in seen if s[1] == "/api/health/db"], [("GET", "/api/health/db", None)])

    def test_rejected_token_is_reported_as_a_status_not_a_value(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = switch.IN_POD.replace("127.0.0.1:8080", f"127.0.0.1:{server.server_address[1]}")
        result = subprocess.run([sys.executable, "-c", script, "get"], capture_output=True, text=True,
                                env={**os.environ, "ADMIN_TOKEN": CANARY}, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), {"status": 401, "write_blocked": None})

    def test_rejected_switch_call_makes_the_command_unknown(self):
        cluster = FakeCluster()
        original = cluster.__call__

        def unauthorised(args):
            out = original(args)
            if args[-1] == "get" and args[3] == "demo-chat-b":
                return json.dumps({"status": 401, "write_blocked": None})
            return out

        code, report = run(unauthorised, "status")
        self.assertEqual(code, 3)
        self.assertIn("demo-chat-b", report["reason"])


class ContractTests(unittest.TestCase):
    def test_names_match_the_rendered_app_and_pooler(self):
        def rendered(*argv):
            out = subprocess.run([sys.executable, *argv], capture_output=True, text=True, check=True).stdout
            return {item["kind"]: item for item in json.loads(out)["items"]}

        app = rendered(str(WORKLOADS / "application.py"))
        self.assertEqual(app["Deployment"]["metadata"]["name"], switch.DEPLOYMENT)
        self.assertEqual(switch.APP_SELECTOR, "app=" + app["Deployment"]["spec"]["selector"]["matchLabels"]["app"])
        self.assertEqual(app["Deployment"]["spec"]["template"]["spec"]["containers"][0]["name"], switch.APP_CONTAINER)
        pooler = rendered(str(WORKLOADS / "pooler.py"), "--egress-address", "10.0.0.10",
                          "--egress-service-uid", "0b7c2a5e-5f1d-4c3a-9e8b-1a2b3c4d5e6f")
        self.assertEqual(pooler["ConfigMap"]["metadata"]["name"], switch.POOLER_CONFIGMAP)
        self.assertEqual(switch.POOLER_SELECTOR, "app=" + pooler["Deployment"]["spec"]["selector"]["matchLabels"]["app"])
        container = pooler["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["name"], switch.POOLER_CONTAINER)
        self.assertIn({"name": "config", "mountPath": "/etc/pgbouncer"}, container["volumeMounts"])
        self.assertEqual(switch.POOLER_FILE, "/etc/pgbouncer/pgbouncer.ini")
        self.assertEqual(switch.database_host(pooler["ConfigMap"]["data"]["pgbouncer.ini"]), SOURCE_HOST)
        self.assertEqual(switch.SOURCE_HOST, SOURCE_HOST)
        self.assertEqual(switch.TARGET_HOST, TARGET_HOST)

    def test_exit_codes_follow_the_repository_convention(self):
        self.assertEqual((switch.PASSED, switch.FAILED, switch.INPUT_ERROR, switch.UNKNOWN), (0, 1, 2, 3))

    def test_status_report_is_one_json_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            switch.main(["status"], kubectl=FakeCluster())
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
