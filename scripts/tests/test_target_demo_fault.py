"""S4 demo fault: pooler to 0 only from a recorded, fully-up state; restore the same Deployment."""
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
        sys.modules.pop("render", None)
        spec = importlib.util.spec_from_file_location("target_demo_fault", WORKLOADS / "demo_fault.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


tool = load()


class Cluster:
    def __init__(self, replicas=2, ready=2, uid="u1"):
        self.uid, self.replicas, self.ready, self.calls = uid, replicas, ready, []

    def __call__(self, *args, timeout=30):
        self.calls.append(args)
        if args[:2] == ("get", "deployment"):
            return json.dumps({"metadata": {"uid": self.uid, "generation": 3},
                               "spec": {"replicas": self.replicas},
                               "status": {"readyReplicas": self.ready, "observedGeneration": 3}})
        if args[0] == "scale":
            current = int(next(a for a in args if a.startswith("--current-replicas="))[19:])
            if current != self.replicas:
                raise tool.Unknown("precondition")
            self.replicas = self.ready = int(next(a for a in args if a.startswith("--replicas="))[11:])
        return ""


class DemoFault(unittest.TestCase):
    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "fault.json"

    def run_tool(self, action, cluster):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = tool.main([action, "--record", str(self.record)], kubectl=cluster)
        return code, json.loads(out.getvalue())

    def test_inject_then_clear_restores_recorded_replicas(self):
        cluster = Cluster()
        self.assertEqual(self.run_tool("inject", cluster)[0], 0)
        self.assertEqual(cluster.replicas, 0)
        self.assertEqual(json.loads(self.record.read_text())["pooler"]["replicas"], 2)
        code, line = self.run_tool("clear", cluster)
        self.assertEqual((code, line["state"], cluster.replicas), (0, "cleared", 2))
        self.assertFalse(self.record.exists())
        self.assertTrue(self.record.with_suffix(".cleared.json").exists())

    def test_never_injects_twice_or_from_a_degraded_pooler(self):
        cluster = Cluster()
        self.run_tool("inject", cluster)
        self.assertEqual(self.run_tool("inject", cluster)[0], 1)
        self.setUp()
        degraded = Cluster(replicas=2, ready=1)
        self.assertEqual(self.run_tool("inject", degraded)[0], 1)
        self.assertFalse(any(c[0] == "scale" for c in degraded.calls))

    def test_record_is_written_before_the_scale(self):
        cluster = Cluster()
        seen = []
        original = cluster.__call__

        def spy(*args, timeout=30):
            if args[0] == "scale":
                seen.append(self.record.exists())
            return original(*args, timeout=timeout)
        self.run_tool("inject", spy)
        self.assertEqual(seen, [True])

    def test_clear_refuses_a_replaced_deployment(self):
        cluster = Cluster()
        self.run_tool("inject", cluster)
        cluster.uid = "u2"
        self.assertEqual(self.run_tool("clear", cluster)[0], 1)
        self.assertEqual(cluster.replicas, 0)

    def test_clear_without_record_changes_nothing(self):
        cluster = Cluster()
        self.assertEqual(self.run_tool("clear", cluster)[0], 1)
        self.assertEqual(cluster.calls, [])

    def test_unreadable_cluster_is_unknown(self):
        def broken(*args, timeout=30):
            raise tool.Unknown("kubectl get failed")
        self.assertEqual(self.run_tool("inject", broken)[0], 3)


if __name__ == "__main__":
    unittest.main()
