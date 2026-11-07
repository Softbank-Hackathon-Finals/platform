"""T14 demo-gate:1 on the target: one other account, one narrow token grant, one canary object."""
import ast
import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "application", "t14_gate"):
        sys.modules.pop(shadowed, None)
    application = importlib.import_module("application")
    t14_gate = importlib.import_module("t14_gate")

CANARY = (WORKLOADS / "t14_canary.py").read_text()


def by_kind(items):
    return {(item["kind"], item["metadata"]["name"]): item for item in items}


class AccountTests(unittest.TestCase):
    def test_other_and_runner_accounts_mount_no_token(self):
        items = by_kind(t14_gate.accounts())
        for name in ("t14-other", "t14-runner"):
            with self.subTest(account=name):
                account = items[("ServiceAccount", name)]
                self.assertIs(account["automountServiceAccountToken"], False)
                self.assertEqual(account["metadata"]["namespace"], "target-dev")

    def test_runner_may_only_mint_tokens_for_the_two_compared_accounts(self):
        items = by_kind(t14_gate.accounts())
        (rule,) = items[("Role", "t14-token-minter")]["rules"]
        self.assertEqual(rule, {"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"],
                                "resourceNames": ["demo-chat", "t14-other"]})
        binding = items[("RoleBinding", "t14-token-minter")]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "t14-runner", "namespace": "target-dev"}])
        self.assertEqual(binding["roleRef"]["kind"], "Role")

    def test_the_other_account_is_granted_nothing(self):
        subjects = [s["name"] for item in t14_gate.accounts() if item["kind"] == "RoleBinding" for s in item["subjects"]]
        self.assertNotIn("t14-other", subjects)
        self.assertFalse(any(item["kind"].startswith("Cluster") for item in t14_gate.accounts()))


class CanaryTests(unittest.TestCase):
    def job(self, arch="arm64"):
        return by_kind(t14_gate.canary(arch))[("Job", "target-t14-canary")]["spec"]["template"]

    def test_object_is_the_one_the_registration_names(self):
        tree = ast.parse(CANARY)
        constants = {n.targets[0].id: n.value.value for n in tree.body
                     if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)}
        self.assertEqual((constants["BUCKET"], constants["KEY"]), ("demo-chat-attachments", "attachments/t14-canary.txt"))
        self.assertEqual(constants["ENDPOINT"], "http://seaweedfs.target-dev.svc.cluster.local:8333")

    def test_keys_come_only_from_the_existing_object_secrets(self):
        env = self.job()["spec"]["containers"][0]["env"]
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in env if "valueFrom" in e}
        self.assertEqual(refs, {"BOOTSTRAP_AWS_ACCESS_KEY_ID": "target-object-bootstrap",
                                "BOOTSTRAP_AWS_SECRET_ACCESS_KEY": "target-object-bootstrap",
                                "CLIENT_AWS_ACCESS_KEY_ID": "target-object-client",
                                "CLIENT_AWS_SECRET_ACCESS_KEY": "target-object-client"})
        self.assertFalse(any("value" in e and "KEY" in e["name"] for e in env))

    def test_the_store_policy_lets_this_pod_in(self):
        # objects.py admits only pods labelled bravo-object-client=true.
        self.assertEqual(self.job()["metadata"]["labels"]["bravo-object-client"], "true")

    def test_image_is_the_registered_one_for_the_node(self):
        for arch, policy in (("arm64", "Never"), ("amd64", "IfNotPresent")):
            with self.subTest(arch=arch):
                container = self.job(arch)["spec"]["containers"][0]
                self.assertEqual(container["image"], application.pinned_image(arch))
                self.assertEqual(container["imagePullPolicy"], policy)
                self.assertEqual(self.job(arch)["spec"]["nodeSelector"], {"kubernetes.io/arch": arch})

    def test_runs_unprivileged_without_a_token(self):
        spec = self.job()["spec"]
        self.assertIs(spec["automountServiceAccountToken"], False)
        self.assertEqual(spec["serviceAccountName"], "target-probe")
        self.assertIs(spec["containers"][0]["securityContext"]["readOnlyRootFilesystem"], True)

    def test_anonymous_read_is_observed_not_required(self):
        # The demo starts open (open-demo) so that IAM-01.1 BLOCKs first; the canary must not fail on it.
        self.assertIn('result["pass"] = result["client_read"]', CANARY)
        self.assertIn('"anonymous_read"', CANARY)

    def test_script_prints_no_secret(self):
        self.assertNotIn("print(os.environ", CANARY)
        self.assertNotIn("import traceback", CANARY)

    def test_cli_renders_both_stages(self):
        for stage in ("accounts", "canary"):
            with self.subTest(stage=stage):
                result = subprocess.run([sys.executable, str(WORKLOADS / "t14_gate.py"), stage, "--arch", "arm64"],
                                        capture_output=True, text=True, check=True, cwd=WORKLOADS)
                self.assertEqual(json.loads(result.stdout)["kind"], "List")


if __name__ == "__main__":
    unittest.main()
