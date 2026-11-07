"""T33 findings API on the platform: host prerequisites and the GitOps bundle's boundaries."""

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("platform_security_render", ROOT / "infra/envs/platform-security/render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
BUNDLE = ROOT / "services/security-findings/deploy/platform-gitops"


class PlatformSecurityPrerequisites(unittest.TestCase):
    def test_namespace_volume_and_default_deny(self):
        items = {(o["kind"], o["metadata"]["name"]): o for o in render.render("ip-10-0-0-1.example")}
        namespace = items[("Namespace", "bravo-security")]
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        volume = items[("PersistentVolume", "platform-security-findings-data")]["spec"]
        self.assertEqual((volume["storageClassName"], volume["persistentVolumeReclaimPolicy"]), ("platform-local", "Retain"))
        self.assertEqual(volume["claimRef"], {"namespace": "bravo-security", "name": "bravo-findings-data"})
        self.assertTrue(volume["local"]["path"].startswith("/var/lib/bravo-platform-data/"))
        node = volume["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
        self.assertEqual(node, ["ip-10-0-0-1.example"])
        self.assertEqual(items[("NetworkPolicy", "default-deny")]["spec"], {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})

    def test_rejects_an_unsafe_node_name(self):
        with self.assertRaises(ValueError):
            render.render("node; rm -rf /")


class FindingsBundle(unittest.TestCase):
    """The bundle is YAML; check it as text so this runs without PyYAML (CI repository tools)."""

    def setUp(self):
        self.resources = (BUNDLE / "resources.yaml").read_text()
        self.kustomization = (BUNDLE / "kustomization.yaml").read_text()

    def test_image_is_pinned_by_digest_only(self):
        self.assertRegex(self.kustomization, r"\n    digest: sha256:[0-9a-f]{64}\n")
        self.assertNotIn("newTag", self.kustomization)
        self.assertIn("newName: ghcr.io/softbank-hackathon-finals/platform-security-findings", self.kustomization)

    def test_claim_matches_the_prerequisite_volume(self):
        self.assertIn(f"name: {render.CLAIM}\n  namespace: {render.NS}", self.resources)
        self.assertIn("storageClassName: platform-local", self.resources)

    def test_registry_reaches_the_api_as_a_private_regular_file(self):
        # Secret volume links are refused by the loader; the init copy makes a 0400 file owned by the service UID.
        self.assertIn("BRAVO_FINDINGS_AUTH_FILE, value: /run/bravo-findings-auth/registry.json", self.resources)
        self.assertIn("os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400", self.resources)
        self.assertIn("emptyDir: {medium: Memory", self.resources)

    def test_runs_locked_down_with_one_writer(self):
        self.assertIn("strategy: {type: Recreate}", self.resources)
        self.assertEqual(self.resources.count("readOnlyRootFilesystem: true"), 2)
        self.assertEqual(self.resources.count("drop: [ALL]"), 2)
        self.assertIn("automountServiceAccountToken: false", self.resources)
        self.assertNotRegex(self.resources, r"(?i)token_sha256|bearer|BEGIN [A-Z ]*KEY")

    def test_onprem_ingest_enters_only_through_the_tailnet_device_proxy(self):
        # The proxy Pod of the T44 tailnet-ingress device bravo-findings-tailnet, never a Tailscale address range.
        [policy] = [d for d in self.resources.split("\n---\n")
                    if "kind: NetworkPolicy" in d and "\n  name: bravo-findings\n" in d]
        self.assertIn("tailscale.com/parent-resource-type: svc\n"
                      "              tailscale.com/parent-resource: bravo-findings-tailnet\n"
                      "              tailscale.com/parent-resource-ns: bravo-security", policy)
        self.assertNotRegex(policy, r"ipBlock|100\.64\.")

    def test_sources_are_the_registered_environments(self):
        sources = json.loads(re.search(r"value: '(\[.*\])'", self.resources).group(1))
        self.assertEqual({(s["source"], s["environment_id"]) for s in sources},
                         {("aws_guardduty", "env-aws"), ("falco", "env-onprem"), ("kyverno", "env-onprem")})


if __name__ == "__main__":
    unittest.main()
