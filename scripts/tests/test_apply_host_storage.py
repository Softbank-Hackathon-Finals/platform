"""Storage boundaries and cross-process receipt persistence, no cluster access."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "infra/envs/apply-host"
spec = importlib.util.spec_from_file_location("apply_storage", FOLDER / "render.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ApplyStorage(unittest.TestCase):
    def test_storage_bound_to_one_node_and_claim_without_database_path(self):
        pv, pvc = module.render("fixture-node")["items"]
        self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(pv["spec"]["claimRef"], {
            "namespace": pvc["metadata"]["namespace"], "name": pvc["metadata"]["name"]})
        self.assertEqual(pvc["spec"]["volumeName"], pv["metadata"]["name"])
        self.assertNotIn("bravo-platform-data", json.dumps(pv))
        with self.assertRaises(ValueError):
            module.render("../not-a-node")

    def test_second_process_reads_same_bytes_and_duplicate_write_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            def execute(mode, uid):
                return subprocess.run([sys.executable, str(FOLDER / "probe.py"), mode,
                    "--root", root, "--probe-id", "fixture"], capture_output=True,
                    env=os.environ | {"POD_UID": uid}, check=False)
            first = execute("write", "writer-fixture")
            self.assertEqual(first.returncode, 0, first.stderr)
            second = execute("read", "reader-fixture")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(first.stdout)["sha256"], json.loads(second.stdout)["sha256"])
            self.assertNotEqual(execute("write", "duplicate-fixture").returncode, 0)
            self.assertNotEqual(execute("read", "writer-fixture").returncode, 0)


if __name__ == "__main__":
    unittest.main()
