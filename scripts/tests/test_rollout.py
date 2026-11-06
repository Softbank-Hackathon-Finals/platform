"""operations/rollout/run.py: a failed step never leaves merges paused."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("rollout", ROOT / "operations/rollout/run.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class Rollout(unittest.TestCase):
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
        rollout.time.sleep = lambda _s: None
        with tempfile.TemporaryDirectory() as d:
            values = Path(d) / "v.json"
            values.write_text(json.dumps({"registration": {}}))
            code = rollout.main(["--instance-id", "i-0123456789abcdef0", "--cluster-uid", "0" * 8 + "-" + "0" * 27,
                                 "--runtime-values", str(values), "--pin-pr", "1", "--apply"])
        sets = [c[-1] for c in calls if c[:3] == ["gh", "variable", "set"]]
        self.assertEqual((code, sets), (1, ["false", "true"]))

    def test_exact_identities_are_required(self):
        with self.assertRaises(SystemExit):
            rollout.main(["--instance-id", "i-1", "--cluster-uid", "x", "--runtime-values", "/v"])


if __name__ == "__main__":
    unittest.main()
