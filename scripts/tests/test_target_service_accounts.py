"""T30 workloads in target-dev run on their own accounts and never mount a Kubernetes API token.

kube-bench rke2-cis-1.9 failed 5.1.5 and 5.1.6 on the demo node (2026-11-06). Both rules are mirrored
here from kube-bench's audit, so a render that would fail them fails this test first.
"""
import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
# Other environments also have modules called render and objects; load this directory's own.
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "replication", "objects", "pooler", "probes", "application", "attachment_job",
                     "identity"):
        sys.modules.pop(shadowed, None)
    render = importlib.import_module("render")
    application = importlib.import_module("application")
    pooler = importlib.import_module("pooler")
    objects = importlib.import_module("objects")
    probes = importlib.import_module("probes")
    attachment_job = importlib.import_module("attachment_job")
    identity = importlib.import_module("identity")

SERVICE_UID = "0b7c2a5e-5f1d-4c3a-9e8b-1a2b3c4d5e6f"
COPY_IMAGE = "registry.example.invalid/bravo/attachment-copy@sha256:" + "a" * 64
PROBE_STAGES = ("db", "objects-bootstrap", "objects-write", "objects-read", "image", "pooler")


def rendered_sources():
    """What each T30 renderer outputs for target-dev, keyed by the renderer."""
    return {
        "application": application.application(application.pinned_image()),
        "pooler": pooler.pooler("10.0.0.10", SERVICE_UID),
        "objects": objects.objects("node-a"),
        "migration": render.migration(),
        "probes": [item for stage in PROBE_STAGES for item in probes.probe(stage, pooler_ip="10.0.0.20")],
        "attachment copy": [attachment_job.job(COPY_IMAGE, "demo-2026-11-07-r1", "Plan_2026-11-07", 3,
                                               "10.43.0.25", SERVICE_UID)],
        "identity": identity.identity("node-a", "https://login.example.com", "https://chat.example.com"),
    }


def pod_templates():
    """(renderer, workload name, Pod spec) for every Deployment and Job."""
    return [(source, item["metadata"]["name"], item["spec"]["template"]["spec"])
            for source, items in rendered_sources().items()
            for item in items if item["kind"] in ("Deployment", "Job")]


def accounts():
    return {item["metadata"]["name"]: item for item in render.accounts()}


def token_mounted(pod, account):
    """kube-bench 5.1.6: an account that leaves the token unset counts as mounting it, whatever the Pod says."""
    on_account = (account or {}).get("automountServiceAccountToken")
    on_pod = pod.get("automountServiceAccountToken")
    if on_account is False and on_pod in (False, None):
        return False
    return not (on_account is True and on_pod is False)


class KubeBenchRuleTests(unittest.TestCase):
    def test_the_mirrored_rule_reproduces_the_demo_node_finding(self):
        # Every T30 Pod already set false itself; on the namespace's unset default account that still failed.
        self.assertTrue(token_mounted({"automountServiceAccountToken": False}, {}))
        self.assertTrue(token_mounted({}, {"automountServiceAccountToken": True}))
        self.assertFalse(token_mounted({"automountServiceAccountToken": False}, {"automountServiceAccountToken": False}))
        self.assertFalse(token_mounted({}, {"automountServiceAccountToken": False}))
        self.assertTrue(token_mounted({"automountServiceAccountToken": True}, {"automountServiceAccountToken": False}))

    def test_default_account_of_the_namespace_mounts_no_token(self):
        # 5.1.5: every namespace but kube-system; RKE2's CIS mode covers only default, kube-public and kube-node-lease.
        default = accounts()["default"]
        self.assertEqual(default["metadata"]["namespace"], render.NAMESPACE)
        self.assertIs(default["automountServiceAccountToken"], False)

    def test_no_workload_runs_on_the_default_account_or_mounts_a_token(self):
        found = accounts()
        for source, name, pod in pod_templates():
            with self.subTest(source=source, workload=name):
                account = pod.get("serviceAccountName")
                self.assertIsInstance(account, str)
                self.assertNotEqual(account, "default")
                self.assertIn(account, found)
                self.assertFalse(token_mounted(pod, found[account]))
                # The Pod keeps its own false too, so a later account change cannot hand it a token.
                self.assertIs(pod["automountServiceAccountToken"], False)


class AccountTests(unittest.TestCase):
    def test_each_renderer_has_an_account_of_its_own(self):
        used = {}
        for source, _, pod in pod_templates():
            used.setdefault(source, set()).add(pod["serviceAccountName"])
        for source, names in used.items():
            with self.subTest(source=source):
                self.assertEqual(len(names), 1)
        owners = [next(iter(names)) for names in used.values()]
        self.assertEqual(len(owners), len(set(owners)))
        self.assertEqual(set(owners), set(accounts()) - {"default"})

    def test_accounts_stage_holds_only_tokenless_accounts(self):
        for name, account in accounts().items():
            with self.subTest(account=name):
                self.assertEqual(account["apiVersion"], "v1")
                self.assertEqual(account["kind"], "ServiceAccount")
                self.assertEqual(account["metadata"]["namespace"], render.NAMESPACE)
                self.assertEqual(account["metadata"]["labels"]["component"], "target")
                self.assertIs(account["automountServiceAccountToken"], False)
                self.assertEqual(set(account), {"apiVersion", "kind", "metadata", "automountServiceAccountToken"})

    def test_no_t30_renderer_grants_an_account_a_role(self):
        kinds = {item["kind"] for items in rendered_sources().values() for item in items}
        self.assertFalse(kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})

    def test_cli_renders_the_accounts_stage_without_a_node(self):
        result = subprocess.run([sys.executable, str(WORKLOADS / "render.py"), "accounts"],
                                capture_output=True, text=True, check=True)
        listing = json.loads(result.stdout)
        self.assertEqual(listing["kind"], "List")
        self.assertEqual({item["metadata"]["name"] for item in listing["items"]}, set(accounts()))


if __name__ == "__main__":
    unittest.main()
