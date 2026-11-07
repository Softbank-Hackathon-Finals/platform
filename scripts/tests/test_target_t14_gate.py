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
    def test_the_other_account_mounts_no_token(self):
        account = by_kind(t14_gate.accounts())[("ServiceAccount", "t14-other")]
        self.assertIs(account["automountServiceAccountToken"], False)
        self.assertEqual(account["metadata"]["namespace"], "target-dev")

    def test_nothing_in_the_cluster_may_mint_tokens(self):
        # RBAC cannot bound a TokenRequest's audience or lifetime; the collector mints from the operator's machine.
        self.assertEqual([(i["kind"], i["metadata"]["name"]) for i in t14_gate.accounts()], [("ServiceAccount", "t14-other")])

    def test_the_other_account_is_granted_nothing(self):
        subjects = [s["name"] for item in t14_gate.accounts() if item["kind"] == "RoleBinding" for s in item["subjects"]]
        self.assertNotIn("t14-other", subjects)
        self.assertFalse(any(item["kind"].startswith("Cluster") for item in t14_gate.accounts()))


class CollectorTests(unittest.TestCase):
    """ADR-0041: the platform's identity, outside the cluster's workloads, with named resources only."""

    def test_only_the_platform_identity_may_mint_and_only_the_two_accounts(self):
        items = t14_gate.collector()
        bindings = [i for i in items if i["kind"] == "RoleBinding"]
        self.assertEqual({s["namespace"] + "/" + s["name"] for b in bindings for s in b["subjects"]},
                         {"bravo-apply-access/bravo-t14-collector"})
        token = [r for i in items if i["kind"] == "Role" for r in i["rules"] if r["resources"] == ["serviceaccounts/token"]]
        self.assertEqual(token, [{"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"],
                                  "resourceNames": ["demo-chat", "t14-other"]}])

    def test_no_cluster_scope_no_wildcards_and_secrets_by_name(self):
        items = t14_gate.collector()
        self.assertFalse(any(i["kind"].startswith("Cluster") for i in items))
        for rule in (r for i in items if i["kind"] == "Role" for r in i["rules"]):
            self.assertNotIn("*", rule["verbs"] + rule["resources"])
            if "secrets" in rule["resources"]:
                self.assertEqual(rule["resourceNames"], ["target-object-client"])
        account = by_kind(items)[("ServiceAccount", "bravo-t14-collector")]
        self.assertIs(account["automountServiceAccountToken"], False)


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
        # The client identity has Write; the bootstrap (Admin) key never enters this Job.
        self.assertEqual(refs, {"CLIENT_AWS_ACCESS_KEY_ID": "target-object-client",
                                "CLIENT_AWS_SECRET_ACCESS_KEY": "target-object-client"})
        self.assertFalse(any("value" in e and "KEY" in e["name"] for e in env))

    def test_bytes_equal_the_source_canary(self):
        # The cutover copy refuses a destination object whose bytes differ from the source's.
        source = (WORKLOADS.parent / "source-workloads/main.tf").read_text()
        block = source[source.index('resource "aws_s3_object" "t14"'):]
        tree = ast.parse(CANARY)
        body = next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "BODY")
        self.assertIn('key     = "attachments/t14-canary.txt"', block[:300])
        self.assertIn(f'content = {json.dumps(body.decode())}', block[:300])

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
