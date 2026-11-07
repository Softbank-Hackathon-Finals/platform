# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Public entry: read-only Argo UI and the reconciler Application only with the reviewed development values."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/gitops/argocd"
ENV = ROOT / "infra/gitops/environments/development/public-entry.json"
sys.path.insert(0, str(ROOT / "services/public-entry/src"))
from bravo_public_entry.reconcile import validate


def render(*extra):
    out = subprocess.run(["helm", "template", "bravo-gitops", str(CHART), "--namespace", "bravo-gitops",
                          "--kube-version", "1.35.9", *extra], check=True, capture_output=True, text=True, timeout=60)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def kind(docs, k, name=None):
    return [d for d in docs if d["kind"] == k and (name is None or d["metadata"]["name"] == name)]


class PublicEntryChart(unittest.TestCase):
    def test_off_by_default(self):
        docs = render()
        self.assertFalse(any(d["metadata"]["name"].startswith(("platform-public-entry", "platform-gitops-public-entry")) for d in docs))
        self.assertFalse(kind(docs, "Deployment", "bravo-gitops-server"))

    def test_development_values_open_a_read_only_ui_and_one_bounded_application(self):
        docs = render("--values", str(ENV))
        self.assertTrue(kind(docs, "Deployment", "bravo-gitops-server"))
        cm = kind(docs, "ConfigMap", "argocd-cm")[0]["data"]
        self.assertEqual((cm["admin.enabled"], cm["exec.enabled"], cm["users.anonymous.enabled"]), ("false", "false", "true"))
        self.assertEqual(kind(docs, "ConfigMap", "argocd-rbac-cm")[0]["data"]["policy.default"], "role:readonly")
        self.assertFalse(kind(docs, "ClusterRole") + kind(docs, "ClusterRoleBinding"))

        app = kind(docs, "Application", "platform-public-entry")[0]["spec"]
        self.assertEqual((app["project"], app["source"]["path"], app["destination"]["namespace"]),
                         ("platform-public-entry", "infra/envs/public-entry", "platform-state"))
        project = kind(docs, "AppProject", "platform-public-entry")[0]["spec"]
        self.assertEqual(project["clusterResourceWhitelist"], [])
        self.assertEqual([d["namespace"] for d in project["destinations"]], ["platform-state"])
        self.assertCountEqual([w["kind"] for w in project["namespaceResourceWhitelist"]],
                              ["Deployment", "ConfigMap", "NetworkPolicy", "ExternalSecret"])
        role = kind(docs, "Role", "platform-gitops-public-entry")[0]
        named = {n for r in role["rules"] if "resourceNames" in r for n in r["resourceNames"]}
        self.assertEqual(named, {"public-entry", "public-entry-spec", "public-entry-cloudflare"})
        self.assertFalse(any("secrets" in r["resources"] for r in role["rules"]))
        policy = kind(docs, "ValidatingAdmissionPolicy", "platform-gitops-registration-names")[0]
        self.assertIn("'public-entry'", policy["spec"]["validations"][0]["expression"])

    def test_workload_holds_no_api_credential_and_pins_the_image(self):
        out = subprocess.run(["kubectl", "kustomize", str(ROOT / "infra/envs/public-entry")], check=True,
                             capture_output=True, text=True, timeout=60)
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        pod = kind(docs, "Deployment", "public-entry")[0]["spec"]["template"]["spec"]
        self.assertEqual((pod["serviceAccountName"], pod["automountServiceAccountToken"]), ("platform-runtime", False))
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["tunnel"]["emptyDir"]["medium"], "Memory")
        self.assertFalse(any("projected" in v or "hostPath" in v for v in pod["volumes"]))
        reconciler, = pod["initContainers"]
        self.assertEqual(reconciler["restartPolicy"], "Always")
        self.assertRegex(reconciler["image"], r"^ghcr\.io/softbank-hackathon-finals/platform-public-entry@sha256:[0-9a-f]{64}$")
        es = kind(docs, "ExternalSecret", "public-entry-cloudflare")[0]["spec"]
        self.assertEqual(es["secretStoreRef"]["name"], "cloudflare-gitops-reader")
        spec = json.loads(kind(docs, "ConfigMap", "public-entry-spec")[0]["data"]["entries.json"])
        validate(spec)


if __name__ == "__main__":
    unittest.main()
