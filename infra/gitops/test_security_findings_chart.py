# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""T33 findings API in the platform Argo: off by default, and when on bounded to bravo-security and its kinds."""
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/gitops/argocd"
ENV = ROOT / "infra/gitops/environments/development/public-entry.json"
BUNDLE = ROOT / "services/security-findings/deploy/platform-gitops"


def render(*extra):
    out = subprocess.run(["helm", "template", "bravo-gitops", str(CHART), "--namespace", "bravo-gitops",
                          "--kube-version", "1.35.9", "--values", str(ENV), *extra],
                         check=True, capture_output=True, text=True, timeout=60)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


# Turning it on also needs the Argo cluster credentials to cover the namespace (the template refuses otherwise).
ON = ("--set", "securityFindings.enabled=true", "--set-string",
      "argo-cd.configs.clusterCredentials.platform.namespaces=platform-state\\,bravo-observability\\,"
      "platform-telemetry\\,bravo-security")


def named(docs, name):
    return [d for d in docs if d["metadata"]["name"] in (name, "platform-gitops-" + name.removeprefix("platform-"))]


class SecurityFindingsChart(unittest.TestCase):
    def test_off_until_the_prerequisites_exist(self):
        self.assertFalse(named(render(), "platform-security-findings"))

    def test_application_is_bounded_to_its_namespace_and_kinds(self):
        docs = named(render(*ON), "platform-security-findings")
        by_kind = {d["kind"]: d for d in docs}
        self.assertEqual(set(by_kind), {"Application", "AppProject", "Role", "RoleBinding"})
        app = by_kind["Application"]["spec"]
        self.assertEqual(app["source"]["path"], "services/security-findings/deploy/platform-gitops")
        self.assertEqual(app["destination"]["namespace"], "bravo-security")
        self.assertFalse(app["syncPolicy"]["automated"]["prune"])  # stored findings never go with a sync
        project = by_kind["AppProject"]["spec"]
        self.assertEqual([d["namespace"] for d in project["destinations"]], ["bravo-security"])
        self.assertEqual(project["clusterResourceWhitelist"], [])
        self.assertEqual({k["kind"] for k in project["namespaceResourceWhitelist"]},
                         {"Deployment", "CronJob", "Service", "ServiceAccount", "PersistentVolumeClaim", "NetworkPolicy"})
        role = by_kind["Role"]
        self.assertEqual(role["metadata"]["namespace"], "bravo-security")
        # The cluster builder's default-deny is not the bundle's: policies change or go only by name.
        changing = [r for r in role["rules"] if r["resources"] == ["networkpolicies"] and "delete" in r["verbs"]]
        self.assertEqual([r.get("resourceNames") for r in changing], [["bravo-findings", "bravo-findings-pull"]])
        self.assertFalse([r for r in role["rules"] if "secrets" in r["resources"]])

    def test_enabling_without_the_argo_cluster_namespace_is_refused(self):
        with self.assertRaises(subprocess.CalledProcessError) as refused:
            render("--set", "securityFindings.enabled=true")
        self.assertIn("bravo-security", refused.exception.stderr)

    def test_project_admits_and_argo_tracks_every_kind_the_bundle_ships(self):
        docs = render(*ON)
        project = named(docs, "platform-security-findings")
        allowed = {k["kind"] for d in project if d["kind"] == "AppProject" for k in d["spec"]["namespaceResourceWhitelist"]}
        shipped = {d["kind"] for d in yaml.safe_load_all((BUNDLE / "resources.yaml").read_text()) if d}
        self.assertLessEqual(shipped, allowed)
        # A kind outside resource.inclusions is skipped by Argo (ExcludedResourceWarning): no PVC, Pending API.
        [cm] = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "argocd-cm"]
        tracked = {k for row in yaml.safe_load(cm["data"]["resource.inclusions"]) for k in row["kinds"]}
        self.assertLessEqual(shipped, tracked)
        self.assertTrue(all(d["metadata"]["namespace"] == "bravo-security"
                            for d in yaml.safe_load_all((BUNDLE / "resources.yaml").read_text()) if d))


    def test_anonymous_viewer_cannot_open_the_application(self):
        # Pod manifests, volume names, events and failed Jobs would show where and whether detection runs.
        [rbac] = [d for d in render() if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "argocd-rbac-cm"]
        self.assertIn("p, role:platform-viewer, applications, get, platform-security-findings/*, deny",
                      rbac["data"]["policy.csv"])


if __name__ == "__main__":
    unittest.main()
