"""T28 reset: read every starting-state item; restore only the fault and the target writes."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"


def load():
    with patch.dict(sys.modules), patch.object(sys, "path", [str(WORKLOADS), *sys.path]):
        for name in ("render", "write_switch", "demo_fault", "object_exposure", "pooler"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("target_demo_reset", WORKLOADS / "demo_reset.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


tool = load()


class Fake:
    def __init__(self, writes="closed", fault=False, bucket="open", ready=1):
        self.writes, self.fault, self.bucket, self.ready, self.calls = writes, fault, bucket, ready, []

    def switch(self, argv):
        self.calls.append(("switch", argv[0]))
        if argv[0] == "close":
            self.writes = "closed"
        print(json.dumps({"action": argv[0], "state": self.writes}))
        return 0

    def fault_main(self, argv, kubectl=None):
        self.calls.append(("fault", argv[0]))
        if argv[0] == "clear":
            self.fault = False
            Path(argv[argv.index("--record") + 1]).unlink()
        print(json.dumps({"action": argv[0], "state": "injected" if self.fault else "clear",
                          "pooler": {"replicas": 1, "ready": self.ready}}))
        return 0

    def probe(self, endpoint, bucket, key):
        self.calls.append(("probe", key))
        return {"state_observed": self.bucket}


class DemoReset(unittest.TestCase):
    def run_tool(self, fake, action, *extra):
        record = Path(tempfile.mkdtemp()) / "fault.json"
        if fake.fault:
            record.write_text("{}")
        with patch.object(tool.write_switch, "main", fake.switch), patch.object(tool.demo_fault, "main", fake.fault_main), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = tool.main([action, "--fault-record", str(record), *extra], probe=fake.probe)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_ready_state_passes_and_check_changes_nothing(self):
        fake = Fake()
        code, line = self.run_tool(fake, "check", "--object-endpoint", "http://s3:8333", "--probe-key", "a/b")
        self.assertEqual((code, line["result"]), (0, "checked_items_ready"))
        self.assertFalse(any(c[1] in ("close", "clear") for c in fake.calls))

    def test_check_reports_each_wrong_item_without_fixing(self):
        for fake in (Fake(writes="open"), Fake(fault=True), Fake(bucket="closed"), Fake(ready=0)):
            with self.subTest(fake=vars(fake)):
                code, _ = self.run_tool(fake, "check", "--object-endpoint", "http://s3:8333", "--probe-key", "a/b")
                self.assertEqual(code, 1)
                self.assertFalse(any(c[1] in ("close", "clear") for c in fake.calls))

    def test_apply_clears_fault_and_closes_writes_but_never_opens_the_bucket(self):
        fake = Fake(writes="open", fault=True, bucket="closed")
        code, line = self.run_tool(fake, "apply", "--object-endpoint", "http://s3:8333", "--probe-key", "a/b")
        self.assertIn(("fault", "clear"), fake.calls)
        self.assertIn(("switch", "close"), fake.calls)
        self.assertEqual((fake.writes, fake.fault, fake.bucket), ("closed", False, "closed"))
        self.assertEqual(code, 1)  # the bucket is still the operator's step
        self.assertEqual({i["name"]: i["ok"] for i in line["items"]}["attachments_bucket"], False)

    def test_unchecked_items_are_reported_not_passed(self):
        code, line = self.run_tool(Fake(), "check")
        observed = {i["name"]: i["observed"] for i in line["items"]}
        self.assertEqual(observed["attachments_bucket"], "not_checked")
        self.assertEqual(observed["edge_weights"], "not_checked (T17)")
        self.assertEqual((code, line["result"]), (0, "checked_items_ready"))


if __name__ == "__main__":
    unittest.main()
