"""disable_on_error per direction: the forward retries on its own, the reverse stops on the first error."""
import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR / "testing"))
replication = importlib.import_module("replication")

# Output of replication.py before direction-specific settings existed, both directions with disable_on_error on.
# They are spelled out instead of being derived, so that only the one intended value can move.
FORWARD_BEFORE = """{
  "apiVersion": "postgresql.cnpg.io/v1",
  "kind": "Subscription",
  "metadata": {
    "name": "t8-source-to-target",
    "namespace": "target-dev",
    "labels": {
      "component": "target",
      "app.kubernetes.io/part-of": "bravo-t8-replication"
    }
  },
  "spec": {
    "cluster": {
      "name": "target-db"
    },
    "dbname": "chat",
    "name": "t8_source_to_target",
    "externalClusterName": "t8-source",
    "publicationName": "source_chat",
    "subscriptionReclaimPolicy": "retain",
    "parameters": {
      "enabled": "true",
      "copy_data": "true",
      "create_slot": "true",
      "slot_name": "t8_source_to_target",
      "origin": "none",
      "binary": "false",
      "streaming": "on",
      "disable_on_error": "true",
      "password_required": "true",
      "run_as_owner": "false"
    }
  }
}
"""
REVERSE_BEFORE = """{
  "apiVersion": "postgresql.cnpg.io/v1",
  "kind": "Subscription",
  "metadata": {
    "name": "t8-target-to-source",
    "namespace": "source-dev",
    "labels": {
      "component": "source",
      "app.kubernetes.io/part-of": "bravo-t8-replication"
    }
  },
  "spec": {
    "cluster": {
      "name": "source-db"
    },
    "dbname": "chat",
    "name": "t8_target_to_source",
    "externalClusterName": "t8-target",
    "publicationName": "target_chat",
    "subscriptionReclaimPolicy": "retain",
    "parameters": {
      "enabled": "false",
      "copy_data": "false",
      "create_slot": "true",
      "slot_name": "t8_target_to_source",
      "origin": "none",
      "binary": "false",
      "streaming": "on",
      "disable_on_error": "true",
      "password_required": "true",
      "run_as_owner": "false"
    }
  }
}
"""


def run_cli(*arguments):
    # A separate process, because these are the bytes an operator pipes into kubectl.
    return subprocess.run([sys.executable, "replication.py", *arguments], cwd=TOOL_DIR, capture_output=True, text=True, check=False)


def only_change(before, after):
    """Paths that differ between two nested dictionaries."""
    if isinstance(before, dict) and isinstance(after, dict):
        found = []
        for key in before.keys() | after.keys():
            found += [f"{key}.{path}" if path else key for path in only_change(before.get(key), after.get(key))]
        return found
    return [] if before == after else [""]


class DirectionTests(unittest.TestCase):
    def test_the_forward_default_turns_off_that_one_value_and_nothing_else(self):
        # The running forward subscription and its CNPG object already say false. A render that said true
        # would turn it back on at the next object change.
        done = run_cli("subscription", "--side", "target")
        self.assertEqual(done.returncode, 0, done.stderr)
        after = json.loads(done.stdout)
        self.assertEqual(only_change(json.loads(FORWARD_BEFORE), after), ["spec.parameters.disable_on_error"])
        self.assertEqual(after["spec"]["parameters"]["disable_on_error"], "false")

    def test_the_reverse_default_is_byte_identical(self):
        self.assertEqual(run_cli("subscription", "--side", "source").stdout, REVERSE_BEFORE)

    def test_stating_the_default_changes_no_byte(self):
        self.assertEqual(run_cli("subscription", "--side", "target", "--disable-on-error", "false").stdout,
                         run_cli("subscription", "--side", "target").stdout)
        self.assertEqual(run_cli("subscription", "--side", "source", "--disable-on-error", "true").stdout, REVERSE_BEFORE)

    def test_the_forward_can_still_be_rendered_as_before(self):
        self.assertEqual(run_cli("subscription", "--side", "target", "--disable-on-error", "true").stdout, FORWARD_BEFORE)

    def test_the_function_and_the_command_line_agree(self):
        for side in ("target", "source"):
            with self.subTest(side=side):
                self.assertEqual(replication.subscription(side), json.loads(run_cli("subscription", "--side", side).stdout))

    def test_the_reverse_can_be_set_on_its_own(self):
        after = json.loads(run_cli("subscription", "--side", "source", "--disable-on-error", "false").stdout)
        self.assertEqual(only_change(json.loads(REVERSE_BEFORE), after), ["spec.parameters.disable_on_error"])
        self.assertEqual(replication.subscription("target")["spec"]["parameters"]["disable_on_error"], "false")

    def test_only_a_real_boolean_is_accepted(self):
        # "false" as a string is truthy, the mistake that would quietly leave the setting on.
        for value in ("false", "true", 0, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replication.subscription("target", disable_on_error=value)

    def test_the_command_line_refuses_anything_else(self):
        self.assertEqual(run_cli("subscription", "--side", "target", "--disable-on-error", "off").returncode, 2)

    def test_the_subscription_render_never_carries_the_timeouts(self):
        # They are server settings, rendered with each Cluster (test_replication_timeout.py).
        for side in ("target", "source"):
            text = json.dumps(replication.subscription(side))
            self.assertNotIn("wal_receiver_timeout", text)
            self.assertNotIn("wal_sender_timeout", text)


if __name__ == "__main__":
    unittest.main()
