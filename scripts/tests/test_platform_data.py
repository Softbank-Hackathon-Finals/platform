"""Regression boundaries for retained storage and role separation."""
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"infra/envs/platform-data/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VolumeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.mount = load("mount_volume")
        self.disk = {"name": "nvme1n1", "serial": "vol0123456789abcdef0", "type": "disk", "mountpoints": [None]}

    def test_matches_exact_ebs_serial(self):
        self.assertEqual(self.mount.select_disk([{"serial": None}, self.disk], "vol-0123456789abcdef0"), self.disk)

    def test_rejects_root_or_other_mount(self):
        for path in ("/", "/var/lib/rancher", "/data"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.mount.select_disk([self.disk | {"mountpoints": [path]}], "vol-0123456789abcdef0")

    def test_rejects_ambiguous_partitioned_and_wrong_disks(self):
        cases = [[self.disk, self.disk], [], [self.disk | {"children": [{"name": "partition"}]}],
                 [self.disk | {"type": "part"}]]
        for disks in cases:
            with self.subTest(disks=disks), self.assertRaises(ValueError):
                self.mount.select_disk(disks, "vol-0123456789abcdef0")

    def test_rejects_shell_input(self):
        with self.assertRaises(ValueError):
            self.mount.select_disk([self.disk], "vol-0123456789abcdef0;echo unsafe")


class WorkloadBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.render = load("render")
        self.items = self.render.render("fixture-node", "--bootstrap", "--migration")["items"]

    def test_retains_separate_node_bound_volume(self):
        pv = next(i for i in self.items if i["kind"] == "PersistentVolume")["spec"]
        self.assertEqual(pv["persistentVolumeReclaimPolicy"], "Retain")
        self.assertIn("local", pv)
        self.assertNotIn("hostPath", pv)
        self.assertEqual(pv["claimRef"], {"namespace": "platform-state", "name": "platform-db-1"})

    def test_runtime_and_migrator_are_distinct_non_privileged_logins(self):
        cluster = next(i for i in self.items if i["kind"] == "Cluster")["spec"]
        roles = cluster["managed"]["roles"]
        self.assertEqual(len({r["passwordSecret"]["name"] for r in roles}), 2)
        for role in roles:
            self.assertFalse(any(role[k] for k in ("superuser", "createdb", "createrole", "replication", "bypassrls")))
            self.assertEqual(len(role["inRoles"]), 1)
        self.assertFalse(cluster["enableSuperuserAccess"])

    def test_no_secret_payload_or_auto_token_and_distinct_namespaces(self):
        self.assertFalse(any(i["kind"] == "Secret" for i in self.items))
        accounts = [i for i in self.items if i["kind"] == "ServiceAccount"]
        self.assertEqual(len({i["metadata"]["namespace"] for i in accounts}), 3)
        self.assertTrue(all(i["automountServiceAccountToken"] is False for i in accounts))

    def test_db_rejects_plaintext_and_network_denies_by_default(self):
        cluster = next(i for i in self.items if i["kind"] == "Cluster")["spec"]
        self.assertEqual(cluster["postgresql"]["pg_hba"][0], "hostnossl all all all reject")
        deny = [i for i in self.items if i["kind"] == "NetworkPolicy" and i["metadata"]["name"] == "default-deny"]
        self.assertEqual(len(deny), 3)
        for policy in deny:
            self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
            self.assertNotIn("ingress", policy["spec"])

    def test_remote_roles_never_grant_exec_secrets_or_escalation(self):
        items = load("access_roles").render("source-fixture", "pooler-fixture")["items"]
        roles = {r["metadata"]["name"]: r for r in items if r["kind"] == "Role"}
        for role in roles.values():
            for rule in role["rules"]:
                self.assertFalse({"*", "secrets", "pods/exec", "serviceaccounts/token", "roles", "rolebindings"} & set(rule["resources"]))
                self.assertFalse({"*", "create", "delete", "bind", "escalate", "impersonate"} & set(rule["verbs"]))
        collector = roles["bravo-collector"]
        self.assertTrue(all(set(r["verbs"]) <= {"get", "list", "watch"} for r in collector["rules"]))
        writes = [r for r in roles["bravo-executor"]["rules"] if "patch" in r["verbs"]]
        self.assertEqual(writes, [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["pooler-fixture"], "verbs": ["get", "patch"]}])


class CleanupBoundaryTests(unittest.TestCase):
    def test_failed_attempt_is_not_terminal_and_does_not_allow_cleanup(self):
        cleanup = load("cleanup_probes")
        self.assertFalse(cleanup.terminal({"status": {"failed": 1, "active": 1}}))
        self.assertTrue(cleanup.terminal({"status": {"conditions": [{"type": "Complete", "status": "True"}]}}))

    def test_preserves_bundles_and_foreign_active_probe_policy(self):
        cleanup = load("cleanup_probes")
        owned = {"metadata": {"name": "t7-db-owned"}, "spec": {"template": {
            "metadata": {"labels": {"bravo.dev/probe": "t7-database"}},
            "spec": {"volumes": [{"configMap": {"name": "shared-bundle"}}]}}},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        active = owned | {"status": {"active": 1}}
        calls = []

        def kube(*args):
            calls.append(args)
            if args[:2] == ("get", "job"):
                return json.dumps(owned)
            if args[:2] == ("get", "jobs"):
                return json.dumps({"items": [active]})
            return ""

        with patch.object(cleanup, "kube", kube), patch.object(cleanup.sys, "stdin", io.StringIO('{"jobs":["t7-db-owned"]}')), patch("sys.stdout", io.StringIO()):
            cleanup.main()
        self.assertEqual([c for c in calls if c[0] == "delete"],
                         [("delete", "job", "t7-db-owned", "--wait=true", "--timeout=60s")])


if __name__ == "__main__":
    unittest.main()
