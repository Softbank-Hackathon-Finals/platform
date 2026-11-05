"""Both databases wait 30 seconds before they call a quiet replication connection dead.

At 5 seconds, CNPG's default, a few seconds of congestion on the relayed tailnet path ended the forward
connection on both ends at once (2026-11-06 03:45 and 03:49 KST). The timeouts are server settings, so they
live in each Cluster render, and the two renders must agree.
"""
import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(TOOL_DIR))
target_render = importlib.import_module("render")
replication = importlib.import_module("replication")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


source_render = module("timeout_source_render", "infra/envs/source-workloads/render.py")
TIMEOUTS = {"wal_sender_timeout": "30s", "wal_receiver_timeout": "30s"}
# Parameters each render carried before this change. Nothing else may move.
SHARED_BEFORE = {"wal_level": "logical", "password_encryption": "scram-sha-256", "log_connections": "on",
                 "log_disconnections": "on", "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "off", "log_statement": "none"}


def cluster(objects):
    return next(item for item in objects if item["kind"] == "Cluster")


class TimeoutRenderTests(unittest.TestCase):
    def setUp(self):
        self.parameters = {"source": cluster(source_render.database())["spec"]["postgresql"]["parameters"],
                           "target": cluster(target_render.database("node-a"))["spec"]["postgresql"]["parameters"]}

    def test_both_clusters_wait_30_seconds_in_both_roles(self):
        # Each side sends on one direction and receives on the other, so each needs both values.
        for side, parameters in self.parameters.items():
            with self.subTest(side=side):
                self.assertEqual({key: parameters.get(key) for key in TIMEOUTS}, TIMEOUTS)

    def test_each_direction_gets_30_seconds_on_both_of_its_ends(self):
        # The reverse runs after cutover over the same relayed path, and a lapse there stops it for good,
        # because it keeps disable_on_error on.
        ends = {"forward": (("source", "wal_sender_timeout"), ("target", "wal_receiver_timeout")),
                "reverse": (("target", "wal_sender_timeout"), ("source", "wal_receiver_timeout"))}
        for direction, pair in ends.items():
            for side, key in pair:
                with self.subTest(direction=direction, side=side, key=key):
                    self.assertEqual(self.parameters[side][key], "30s")

    def test_the_two_renders_cannot_drift_apart(self):
        self.assertEqual(source_render.REPLICATION_TIMEOUTS, target_render.REPLICATION_TIMEOUTS)

    def test_only_the_two_timeouts_are_added(self):
        for side, parameters in self.parameters.items():
            with self.subTest(side=side):
                self.assertEqual(parameters, {**SHARED_BEFORE, **TIMEOUTS})

    def test_a_shared_constant_is_not_handed_out_for_editing(self):
        parameters = self.parameters["target"]
        parameters["wal_sender_timeout"] = "5s"
        self.assertEqual(target_render.REPLICATION_TIMEOUTS["wal_sender_timeout"], "30s")
        self.assertEqual(cluster(target_render.database("node-a"))["spec"]["postgresql"]["parameters"]["wal_sender_timeout"], "30s")

    def test_the_subscription_render_does_not_carry_server_settings(self):
        for side in ("target", "source"):
            with self.subTest(side=side):
                text = json.dumps(replication.subscription(side))
                self.assertNotIn("wal_receiver_timeout", text)
                self.assertNotIn("wal_sender_timeout", text)


if __name__ == "__main__":
    unittest.main()
