import ssl
import unittest

import render


def requests():
    return render.cluster("192.0.2.10")["spec"]["selfInit"]["requests"]


class Boundary(unittest.TestCase):
    def test_kv_config_runs_after_mount_upgrade_window(self):
        names = [r["name"] for r in requests()]
        self.assertEqual(names[0], "platform-kv")
        self.assertEqual(names[-1], "platform-cas")

    def test_lifecycle_is_crd_owned_and_does_not_reinitialize_target(self):
        value = render.cluster("192.0.2.10")
        self.assertEqual(value["kind"], "OpenBaoCluster")
        self.assertEqual(value["metadata"]["namespace"], "platform-secrets")
        self.assertEqual(value["spec"]["deletionPolicy"], "Retain")
        self.assertEqual(value["spec"]["controllerJWTMode"], "Target")
        self.assertTrue(value["spec"]["selfInit"]["oidc"]["enabled"])
        self.assertIn("@sha256:", value["spec"]["image"])

    def test_audit_is_declarative_for_openbao_272(self):
        spec = render.cluster("192.0.2.10")["spec"]
        self.assertEqual(spec["audit"], [{"type": "file", "path": "stdout",
                                       "fileOptions": {"filePath": "stdout"}}])
        self.assertFalse(any(r["path"].startswith("sys/audit/") for r in requests()))

    def test_every_policy_names_exact_paths_without_admin_permission(self):
        policies = [r["policy"]["policy"] for r in requests() if "policy" in r]
        self.assertEqual(len(policies), len(render.policies()))
        for policy in policies:
            self.assertNotIn("*", policy)
            self.assertNotIn("sudo", policy)
            self.assertNotIn("sys/", policy)
            self.assertNotIn("auth/", policy)
            self.assertNotIn("delete", policy)

    def test_consumers_read_only_their_own_entries(self):
        rules = render.policies()
        runner = rules[("platform-state", "ai-proposal-runner")]
        self.assertEqual(runner, 'path "platform/data/ai/model-access" { capabilities = ["read"] }')
        self.assertNotIn("model-refresh", runner)
        edge = rules[("platform-state", "t17-edge-secret-reader")]
        self.assertEqual(edge, 'path "platform/data/edge/cloudflare-lb" { capabilities = ["read"] }')

    def test_writers_cannot_read_values_they_write(self):
        rules = render.policies()
        for name in ("t17-edge-key-writer", "cloudflare-gitops-key-writer"):
            policy = rules[("platform-secrets", name)]
            for line in policy.splitlines():
                if "/data/" in line:
                    self.assertNotIn('"read"', line)

    def test_refresh_token_is_isolated_to_the_refresher(self):
        readers = [key for key, policy in render.policies().items()
                   if 'model-refresh" { capabilities = ["read"]' in policy]
        self.assertEqual(readers, [("platform-state", "ai-subscription-refresher")])

    def test_roles_bind_exact_service_account_and_short_ttl(self):
        roles = [r["data"] for r in requests() if r["path"].startswith("auth/")]
        self.assertEqual({r["bound_subject"] for r in roles}, {
            f"system:serviceaccount:{ns}:{name}" for ns, name in render.policies()})
        self.assertTrue(all(r["token_no_default_policy"] and r["token_max_ttl"] == "300s"
                            and r["bound_audiences"] == [render.AUDIENCE] for r in roles))

    def test_operator_ssm_access_is_not_in_the_catalog(self):
        keys = {e.key for e in render.CREDENTIALS}
        self.assertFalse(any("sso" in k or "ssm" in k or "aws" in k for k in keys))

    def test_existing_edge_consumer_secret_name_is_kept(self):
        entry = next(e for e in render.CREDENTIALS if e.key == "edge/cloudflare-lb")
        self.assertEqual((entry.target, entry.properties), ("t17-cloudflare-lb", ("token",)))

    def test_retained_storage_has_exact_new_platform_path(self):
        value = render.foundation("verified-node")
        pv = next(o for o in value["items"] if o["kind"] == "PersistentVolume")
        self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(pv["spec"]["local"]["path"], "/var/lib/bravo-platform/openbao")
        self.assertEqual(pv["spec"]["claimRef"]["name"], "data-platform-openbao-0")
        accounts = {o["metadata"]["name"] for o in value["items"] if o["kind"] == "ServiceAccount"}
        self.assertEqual(accounts, {"t17-edge-key-writer", "cloudflare-gitops-key-writer"})
        with self.assertRaises(ValueError):
            render.foundation("../../existing")

    def test_unverified_ca_and_hostnames_are_not_accepted_as_connection_evidence(self):
        with self.assertRaises(ValueError):
            render.cluster("unverified.example")
        with self.assertRaises(ssl.SSLError):
            render.delivery("-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----")

    def test_catalog_rejects_reused_account_names_and_bad_keys(self):
        with self.assertRaises(ValueError):
            render.Credential("../x", reader="a", writer="b", target="c", properties=("token",))
        clash = (render.Credential("x/y", reader="same", writer="w", target="t", properties=("v",)),
                 render.Credential("x/z", reader="r", writer="same", target="u", properties=("v",)))
        with self.assertRaises(ValueError):
            render.policies(clash)


if __name__ == "__main__":
    unittest.main()
