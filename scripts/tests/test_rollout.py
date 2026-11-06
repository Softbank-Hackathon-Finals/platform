"""operations/rollout/run.py: a failed step never leaves merges paused."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("rollout", ROOT / "operations/rollout/run.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class Rollout(unittest.TestCase):
    def setUp(self):
        # Patch only this module's names; never the shared time module other tests rely on.
        for name in ("sh", "node", "pins"):
            patcher = mock.patch.object(rollout, name, getattr(rollout, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch.object(rollout, "time", mock.Mock(wraps=rollout.time, sleep=lambda _s: None,
                                                                strftime=rollout.time.strftime, gmtime=rollout.time.gmtime))
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_failed_pin_merge_restores_the_queue_variable(self):
        calls = []

        def sh(args, *, check=True):
            calls.append(args)
            if args[:3] == ["gh", "variable", "get"]:
                return "true"
            if args[:3] == ["gh", "pr", "view"]:
                return "CLOSED "
            return ""

        rollout.sh, rollout.node = sh, lambda *_a, **_k: {"status": "PASS"}
        with tempfile.TemporaryDirectory() as d:
            values = Path(d) / "v.json"
            values.write_text(json.dumps({"registration": {}}))
            code = rollout.main(["--instance-id", "i-0123456789abcdef0", "--cluster-uid", "0" * 8 + "-" + "0" * 27,
                                 "--runtime-values", str(values), "--pin-pr", "1", "--apply"])
        sets = [c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]]
        self.assertEqual((code, sets), (1, ["false", "true"]))

    def _run(self, sh, node):
        rollout.sh, rollout.node = sh, node
        with tempfile.TemporaryDirectory() as d:
            values = Path(d) / "v.json"
            values.write_text(json.dumps({"registration": {}}))
            return rollout.main(["--instance-id", "i-0123456789abcdef0", "--cluster-uid", "0" * 8 + "-" + "0" * 27,
                                 "--runtime-values", str(values), "--apply", "--ready-wait", "15"])

    def test_unreadable_queue_variable_stops_before_pausing(self):
        calls = []

        def sh(args, *, check=True):
            calls.append(args)
            if args[:3] == ["gh", "variable", "get"]:
                raise rollout.Failure("gh:auth")
            return ""

        self.assertEqual(self._run(sh, lambda *_a, **_k: {"status": "PASS"}), 1)
        self.assertFalse([c for c in calls if c[:3] == ["gh", "variable", "set"]])

    def test_a_missing_workload_fails_the_readback(self):
        rollout.pins = lambda _r: {"product-reader": "a", "registration": "b"}
        ready = {"ready": 1, "image": "x@b"}

        def node(_i, packet):
            if packet["op"] == "readback":
                return {"workloads": {"t10-registration": ready, "t10-workflow-host": ready}}
            return {"status": "PASS"}

        sh = lambda args, *, check=True: "true" if args[:3] == ["gh", "variable", "get"] else "sha"  # noqa: E731
        self.assertEqual(self._run(sh, node), 1)

    def test_exact_identities_are_required(self):
        with self.assertRaises(SystemExit):
            rollout.main(["--instance-id", "i-1", "--cluster-uid", "x", "--runtime-values", "/v"])


if __name__ == "__main__":
    unittest.main()
