import ssl
import unittest

import render


class Boundary(unittest.TestCase):
    def test_lifecycle_is_crd_owned_and_does_not_reinitialize_target(self):
        value = render.cluster("192.0.2.10")
        self.assertEqual(value["kind"], "OpenBaoCluster")
        self.assertEqual(value["metadata"]["namespace"], "platform-secrets")
        self.assertEqual(value["spec"]["deletionPolicy"], "Retain")
        self.assertEqual(value["spec"]["controllerJWTMode"], "Target")
        self.assertTrue(value["spec"]["selfInit"]["oidc"]["enabled"])
        self.assertIn("@sha256:", value["spec"]["image"])

    def test_exact_reader_writer_subjects_and_path_without_admin_permission(self):
        requests = render.cluster("192.0.2.10")["spec"]["selfInit"]["requests"]
        policies = [r["policy"]["policy"] for r in requests if "policy" in r]
        self.assertEqual(policies[0], 'path "edge-t17/data/cloudflare" { capabilities = ["read"] }')
        self.assertTrue(all("*" not in p and "sudo" not in p for p in policies))
        roles = [r["data"] for r in requests if r["path"].startswith("auth/")]
        self.assertEqual({r["bound_subject"] for r in roles}, {
            "system:serviceaccount:platform-state:t17-edge-secret-reader",
            "system:serviceaccount:platform-secrets:t17-edge-key-writer"})
        self.assertTrue(all(r["token_no_default_policy"] and r["token_max_ttl"] == "300s" for r in roles))

    def test_retained_storage_has_exact_new_platform_path(self):
        value = render.foundation("verified-node")
        pv = next(o for o in value["items"] if o["kind"] == "PersistentVolume")
        self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(pv["spec"]["local"]["path"], "/var/lib/bravo-platform/openbao")
        self.assertEqual(pv["spec"]["claimRef"]["name"], "data-platform-openbao-0")
        with self.assertRaises(ValueError):
            render.foundation("../../existing")

    def test_unverified_ca_and_hostnames_are_not_accepted_as_connection_evidence(self):
        with self.assertRaises(ValueError):
            render.cluster("unverified.example")
        with self.assertRaises(ssl.SSLError):
            render.delivery("-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----")


if __name__ == "__main__":
    unittest.main()
