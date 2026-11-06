"""T31 APPLIED -> one write_switch open: never without a real rollout, never twice."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"


def load():
    with patch.dict(sys.modules), patch.object(sys, "path", [str(WORKLOADS), *sys.path]):
        for name in ("render", "write_switch", "pooler"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("target_open_after_apply", WORKLOADS / "open_after_apply.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


tool = load()
APPLIED = {"application_id": "app-1", "plan_id": "plan-1", "outcome": "APPLIED", "job_passed": True,
           "cluster": "kubectl", "deployed_digest": "sha256:" + "a" * 64,
           "target": {"namespace": "target-dev", "deployment": "demo-chat"}}


class OpenAfterApply(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.calls, self.statuses, self.open_code = [], [], 0

    def fake_switch(self, action, kubeconfig):
        self.calls.append(action)
        if action == "status":
            code = self.statuses.pop(0) if self.statuses else 0
            return code, {"action": "status", "state": "closed"}
        return self.open_code, {"action": "open", "state": "open" if self.open_code == 0 else "closed"}

    def main(self, result=APPLIED, *extra):
        run = lambda *a, **k: SimpleNamespace(stdout=json.dumps(result) + "\n")
        clock = iter(range(0, 10_000, 10))
        with patch.object(tool, "switch", self.fake_switch), contextlib.redirect_stdout(io.StringIO()) as out:
            code = tool.main(["--application-id", "app-1", "--receipts", str(self.dir), *extra],
                             run=run, sleep=lambda _: None, clock=lambda: next(clock))
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_applied_rollout_opens_once_and_records_it(self):
        code, line = self.main()
        self.assertEqual((code, line["opened"], self.calls), (0, True, ["status", "open"]))
        record = json.loads((self.dir / "app-1.write-switch.json").read_text())
        self.assertEqual(record["deployed_digest"], APPLIED["deployed_digest"])
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.calls, ["status", "open"])  # the rerun read the record only

    def test_waits_only_while_status_cannot_confirm(self):
        self.statuses = [3, 3, 0]
        code, line = self.main()
        self.assertEqual((code, line["status_polls"], self.calls[-1]), (0, 3, "open"))

    def test_wrong_state_or_timeout_never_opens(self):
        for statuses, expected in (([1], 1), ([3] * 100, 3)):
            with self.subTest(statuses=statuses):
                self.setUp()
                self.statuses = list(statuses)
                code, line = self.main(APPLIED, "--wait", "30")
                self.assertEqual((code, line["opened"]), (expected, False))
                self.assertNotIn("open", self.calls)

    def test_without_a_real_applied_rollout_nothing_is_called(self):
        for change in ({"outcome": "REFUSED"}, {"outcome": "FAILED", "job_passed": False}, {"cluster": "fake"},
                       {"deployed_digest": None}, {"target": {"namespace": "default", "deployment": "demo-chat"}},
                       {"target": {"namespace": "target-dev", "deployment": "other"}}):
            with self.subTest(change=change):
                self.setUp()
                self.assertEqual(self.main(APPLIED | change)[0], 1)
                self.assertEqual(self.calls, [])

    def test_failed_open_is_recorded_and_not_retried(self):
        self.open_code = 1
        self.assertEqual(self.main()[0], 1)
        self.assertEqual(self.main()[0], 1)
        self.assertEqual(self.calls.count("open"), 1)

    def test_crash_after_marker_never_opens_again(self):
        (self.dir / "app-1.write-switch.started.json").write_text("{}")
        code, line = self.main()
        self.assertEqual((code, line["state"], self.calls), (3, "unknown", []))

    def test_switch_runs_write_switch_in_process_and_keeps_its_json(self):
        def main(argv):
            print(json.dumps({"action": argv[0], "state": "closed"}))
            return 3
        with patch.object(tool.write_switch, "main", main):
            self.assertEqual(tool.switch("status", "/k"), (3, {"action": "status", "state": "closed"}))


if __name__ == "__main__":
    unittest.main()
