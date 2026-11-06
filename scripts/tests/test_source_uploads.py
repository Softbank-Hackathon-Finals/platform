"""출발지 업로드 중지: 업로드만 끄고, 출발지 쓰기는 바꾸기 전 상태로 돌려놓는다."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
spec = importlib.util.spec_from_file_location("source_uploads", WORKLOADS / "source_uploads.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def pod(name, ready=True, terminating=False):
    meta = {"name": name}
    if terminating:
        meta["deletionTimestamp"] = "2026-11-07T00:00:00Z"
    return {"metadata": meta, "status": {"phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class FakeCluster:
    """출발지 앱 하나를 흉내 낸다. 환경변수를 바꾸면 Pod가 새로 뜨고, 새 Pod는 쓰기 막힘으로 시작한다."""

    def __init__(self, write_blocked=False, upload=True, pods=1):
        self.pods = {f"demo-chat-old-{i}": {"write_blocked": write_blocked, "upload": upload} for i in range(pods)}
        self.calls = []
        self.upload_after_restart = None  # 시험에서 새 Pod의 업로드 값을 일부러 틀리게 할 때 쓴다
        self.reopen_status = 200
        self.refuse = set()  # 쓰기 스위치를 거부할 Pod 이름

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["get", "pods"]:
            return json.dumps({"items": [pod(name) for name in self.pods]})
        if args[:2] == ["set", "env"]:
            value = args[-1].split("=", 1)[1] == "true"
            self.pods = {"demo-chat-new-0": {"write_blocked": True,
                         "upload": value if self.upload_after_restart is None else self.upload_after_restart}}
            return ""
        if args[:2] == ["rollout", "status"]:
            return "successfully rolled out"
        if args[0] == "exec":
            name, action = args[1], args[-1]
            state = self.pods[name]
            if action == "info":
                return json.dumps({"status": 200, "write_blocked": state["write_blocked"],
                                   "attachment_upload_enabled": state["upload"]})
            if self.reopen_status != 200 or name in self.refuse:
                return json.dumps({"status": 401, "write_blocked": None})
            state["write_blocked"] = action == "close"
            return json.dumps({"status": 200, "write_blocked": state["write_blocked"]})
        raise AssertionError(f"unexpected kubectl {args}")

    def verbs(self):
        return [" ".join(a[:2]) if a[0] != "exec" else f"exec {a[-1]}" for a in self.calls]


class SourceUploads(unittest.TestCase):
    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "record.jsonl"

    def run_tool(self, cluster, action="stop", target=None, extra=()):
        out = io.StringIO()
        argv = [action, "--record", str(self.record), *extra]
        with contextlib.redirect_stdout(out):
            code = tool.main(argv, kubectl=cluster, target_status=target)
        lines = self.record.read_text().splitlines() if self.record.exists() else []
        return code, json.loads(out.getvalue()), [json.loads(line) for line in lines]

    def test_stop_while_writes_open_reopens_writes_after_the_restart(self):
        cluster = FakeCluster(write_blocked=False)
        code, result, records = self.run_tool(cluster)
        self.assertEqual(code, tool.PASSED)
        self.assertEqual(cluster.pods, {"demo-chat-new-0": {"write_blocked": False, "upload": False}})
        self.assertIn(["set", "env", "deployment/demo-chat", "-n", "source-dev", "-c", "app",
                       "ATTACHMENT_UPLOAD_ENABLED=false"], cluster.calls)
        self.assertIn("exec open", cluster.verbs())
        self.assertEqual(result["before"]["write_blocked"], False)
        self.assertEqual(result["after"], {"write_blocked": False, "attachment_upload_enabled": False})
        self.assertEqual(result["pods_before"], ["demo-chat-old-0"])
        self.assertEqual(result["pods_after"], ["demo-chat-new-0"])
        for key in ("requested_at", "rolled_out_at", "writes_restored_at"):
            self.assertIn(key, result)
        self.assertEqual(records, [result])

    def test_stop_while_writes_blocked_never_opens_writes(self):
        cluster = FakeCluster(write_blocked=True)
        code, result, _ = self.run_tool(cluster)
        self.assertEqual(code, tool.PASSED)
        self.assertNotIn("exec open", cluster.verbs())
        self.assertEqual(cluster.pods["demo-chat-new-0"], {"write_blocked": True, "upload": False})
        self.assertIsNone(result["writes_restored_at"])

    def test_already_stopped_changes_nothing(self):
        cluster = FakeCluster(upload=False)
        code, result, _ = self.run_tool(cluster)
        self.assertEqual(code, tool.PASSED)
        self.assertNotIn("set env", cluster.verbs())
        self.assertTrue(result["unchanged"])

    def test_pods_that_disagree_stop_before_any_change(self):
        cluster = FakeCluster(pods=2)
        cluster.pods["demo-chat-old-1"]["write_blocked"] = True
        code, result, _ = self.run_tool(cluster)
        self.assertEqual(code, tool.UNKNOWN)
        self.assertNotIn("set env", cluster.verbs())
        self.assertIn("error", result)

    def test_new_pod_still_taking_uploads_is_a_failure(self):
        cluster = FakeCluster()
        cluster.upload_after_restart = True
        code, result, _ = self.run_tool(cluster)
        self.assertEqual(code, tool.FAILED)
        self.assertIn("upload", result["error"])

    def test_write_reopen_refused_reports_writes_left_blocked(self):
        cluster = FakeCluster(write_blocked=False)
        cluster.reopen_status = 401
        code, result, _ = self.run_tool(cluster)
        self.assertEqual(code, tool.FAILED)
        self.assertIn("write", result["error"])
        self.assertEqual(result["after"]["write_blocked"], True)

    def test_restore_turns_uploads_back_on_and_keeps_write_state(self):
        cluster = FakeCluster(write_blocked=False, upload=False)
        code, result, _ = self.run_tool(cluster, "restore")
        self.assertEqual(code, tool.PASSED)
        self.assertEqual(result["after"], {"write_blocked": False, "attachment_upload_enabled": True})
        self.assertEqual(cluster.pods["demo-chat-new-0"], {"write_blocked": False, "upload": True})
        self.assertIn(["set", "env", "deployment/demo-chat", "-n", "source-dev", "-c", "app",
                       "ATTACHMENT_UPLOAD_ENABLED=true"], cluster.calls)

    def test_status_reads_only(self):
        cluster = FakeCluster()
        code, result, records = self.run_tool(cluster, "status")
        self.assertEqual(code, tool.PASSED)
        self.assertEqual(cluster.verbs(), ["get pods", "exec info"])
        self.assertEqual(result["pods"], {"demo-chat-old-0": {"write_blocked": False, "attachment_upload_enabled": True}})
        self.assertEqual(records, [])

    def test_token_is_read_inside_the_pod_only(self):
        self.assertIn('os.environ["ADMIN_TOKEN"]', tool.IN_POD)
        cluster = FakeCluster()
        self.run_tool(cluster)
        self.assertFalse(any("ADMIN_TOKEN=" in part for args in cluster.calls for part in args))


CLOSED = {"state": "closed", "checks": {"target_app_role": "nologin", "pooler_target": "source"}}


class OpenSourceWrites(unittest.TestCase):
    """S0: 목적지 쓰기가 닫혀 있을 때만 출발지 쓰기를 연다. 쓰기를 받는 DB는 언제나 하나다."""

    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "record.jsonl"
        self.target_calls = []

    def target(self, state):
        def read(kubeconfig):
            self.target_calls.append(kubeconfig)
            if isinstance(state, Exception):
                raise state
            return state
        return read

    def run_open(self, cluster, state=CLOSED, extra=("--target-kubeconfig", "/private/target.kubeconfig")):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(["open-writes", "--record", str(self.record), *extra],
                             kubectl=cluster, target_status=self.target(state))
        lines = self.record.read_text().splitlines() if self.record.exists() else []
        return code, json.loads(out.getvalue()), [json.loads(line) for line in lines]

    def test_opens_blocked_source_when_target_is_closed(self):
        cluster = FakeCluster(write_blocked=True)
        code, result, records = self.run_open(cluster)
        self.assertEqual(code, tool.PASSED)
        self.assertEqual(cluster.pods["demo-chat-old-0"]["write_blocked"], False)
        self.assertEqual(self.target_calls, ["/private/target.kubeconfig"])
        self.assertEqual(result["after"], {"write_blocked": False, "attachment_upload_enabled": True})
        self.assertEqual(result["target_state"], "closed")
        self.assertIn("writes_opened_at", result)
        self.assertEqual(records, [result])

    def test_target_not_closed_never_opens_source(self):
        for state in ({"state": "open", "checks": {"target_app_role": "nologin"}},
                      {"state": "closed", "checks": {"target_app_role": "login"}},
                      {"state": "mixed", "checks": {}}):
            with self.subTest(state=state):
                cluster = FakeCluster(write_blocked=True)
                code, result, _ = self.run_open(cluster, state)
                self.assertEqual(code, tool.FAILED)
                self.assertNotIn("exec open", cluster.verbs())
                self.assertIn("target", result["error"])

    def test_unreadable_target_never_opens_source(self):
        cluster = FakeCluster(write_blocked=True)
        code, result, _ = self.run_open(cluster, tool.Unknown("target write switch could not be read"))
        self.assertEqual(code, tool.UNKNOWN)
        self.assertEqual(cluster.verbs(), [])
        self.assertIn("could not be read", result["error"])

    def test_already_open_changes_nothing(self):
        cluster = FakeCluster(write_blocked=False)
        code, result, _ = self.run_open(cluster)
        self.assertEqual(code, tool.PASSED)
        self.assertNotIn("exec open", cluster.verbs())
        self.assertTrue(result["unchanged"])

    def test_partial_open_closes_every_pod_again(self):
        cluster = FakeCluster(write_blocked=True, pods=2)
        cluster.refuse = {"demo-chat-old-1"}
        code, result, _ = self.run_open(cluster)
        self.assertEqual(code, tool.FAILED)
        self.assertIn("exec close", cluster.verbs())
        self.assertTrue(all(state["write_blocked"] for state in cluster.pods.values()))
        self.assertEqual(result["after"]["write_blocked"], True)

    def test_target_kubeconfig_is_required(self):
        cluster = FakeCluster(write_blocked=True)
        code, result, _ = self.run_open(cluster, extra=())
        self.assertEqual(code, tool.INPUT_ERROR)
        self.assertEqual(cluster.verbs(), [])
        self.assertIn("--target-kubeconfig", result["error"])


if __name__ == "__main__":
    unittest.main()
