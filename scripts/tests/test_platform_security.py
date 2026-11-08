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
        self.assertIn('os.replace(tmp, "/run/bravo-findings-auth/registry.json")', self.resources)  # survives a sandbox restart
        self.assertIn("emptyDir: {medium: Memory", self.resources)

    def test_runs_locked_down_with_one_writer(self):
        self.assertIn("strategy: {type: Recreate}", self.resources)
        # Every Pod mounting the local PV skips the per-mount chown/chmod, or findings.db becomes 0660 and Store refuses it.
        self.assertEqual(self.resources.count("fsGroupChangePolicy: OnRootMismatch"), self.resources.count("fsGroup: 10001"))
        readme = (ROOT / "infra/envs/platform-security/README.md").read_text()
        self.assertIn("install -d -o 10001 -g 10001 -m 2770", readme)
        # init, api and the GuardDuty and SCC pull containers
        self.assertEqual(self.resources.count("readOnlyRootFilesystem: true"), 4)
        self.assertEqual(self.resources.count("drop: [ALL]"), 4)
        self.assertEqual(self.resources.count("automountServiceAccountToken: false"), 6)  # three SAs and three Pods
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
                         {("aws_guardduty", "env-aws"), ("falco", "env-onprem"), ("kyverno", "env-onprem"),
                          ("gcp_scc", "env-gcp")})

    def test_guardduty_pull_uses_web_identity_only(self):
        # A projected STS-audience token and a role from a ConfigMap outside Git; never a long-lived key.
        self.assertIn("serviceAccountToken: {audience: sts.amazonaws.com, expirationSeconds: 3600, path: token}", self.resources)
        self.assertIn("{name: AWS_WEB_IDENTITY_TOKEN_FILE, value: /var/run/secrets/aws/token}", self.resources)
        self.assertIn("configMapKeyRef: {name: bravo-findings-aws, key: role_arn}", self.resources)
        self.assertNotRegex(self.resources, r"AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_PROFILE|arn[:]aws[:]|\b\d{12}\b")
        self.assertIn("command: [python, -m, bravo_findings.aws_pull]", self.resources)
        self.assertIn("--environment-id, env-aws", self.resources)
        self.assertIn("concurrencyPolicy: Forbid", self.resources)
        self.assertIn("startingDeadlineSeconds: 120", self.resources)
        # 443 out, but not to Pods, Services or link-local (IMDS) of the platform cluster
        self.assertIn("ipBlock: {cidr: 0.0.0.0/0, except: [10.42.0.0/16, 10.43.0.0/16, 169.254.0.0/16]}", self.resources)
        self.assertNotIn("ipBlock: {cidr: 0.0.0.0/0}", self.resources)

    def test_scc_pull_uses_federation_only_and_keeps_numbers_out_of_git(self):
        [job] = [d for d in self.resources.split("\n---\n") if "kind: CronJob" in d and "name: bravo-findings-scc-pull" in d]
        self.assertIn("command: [python, -m, bravo_findings.gcp_pull]", job)
        self.assertIn("args: [--database, /data/findings.db, --environment-id, env-gcp]", job)
        # The provider's only allowed audience (gcp-scc-reader token_audience), not its URL with the project number.
        self.assertIn("serviceAccountToken: {audience: bravo-findings-scc, expirationSeconds: 3600, path: token}", job)
        reader = (ROOT / "infra/envs/gcp-scc-reader/variables.tf")
        if reader.exists():  # the GCP stack lands separately (#940)
            self.assertIn('default = "bravo-findings-scc"', reader.read_text())
        self.assertIn("configMap: {name: bravo-findings-scc", job)
        self.assertIn("concurrencyPolicy: Forbid", job)
        self.assertNotRegex(job, r"(?i)private_key|GOOGLE_CREDENTIALS_JSON|projects/[0-9]|iam\.gserviceaccount|\b\d{6,}\b")
        [policy] = [d for d in self.resources.split("\n---\n")
                    if "kind: NetworkPolicy" in d and "name: bravo-findings-scc-pull" in d]
        self.assertIn("ipBlock: {cidr: 0.0.0.0/0, except: [10.42.0.0/16, 10.43.0.0/16, 169.254.0.0/16]}", policy)
        self.assertIn("ingress: []", policy)

    def test_role_matches_the_puller_subject(self):
        main = (ROOT / "infra/envs/platform-security/main.tf").read_text()
        variables = (ROOT / "infra/envs/platform-security/variables.tf").read_text()
        self.assertIn('default     = "system:serviceaccount:bravo-security:bravo-findings-pull"', variables)
        self.assertIn("name: bravo-findings-pull\n  namespace: bravo-security", self.resources)
        self.assertNotIn('resource "aws_iam_openid_connect_provider"', main)  # read, never create, the issuer provider
        self.assertIn('data "aws_iam_openid_connect_provider" "platform"', main)


if __name__ == "__main__":
    unittest.main()
