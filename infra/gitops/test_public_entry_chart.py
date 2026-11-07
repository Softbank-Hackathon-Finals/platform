# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Public entry: read-only Argo UI and the reconciler Application only with the reviewed development values."""
import json
import re
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
        rbac = kind(docs, "ConfigMap", "argocd-rbac-cm")[0]["data"]
        # Anonymous sees Applications and projects only: no logs, no other resource kinds.
        self.assertEqual(rbac["policy.default"], "role:platform-viewer")
        self.assertNotIn("logs", rbac["policy.csv"])
        # Applications whose values carry operational identifiers (hostAliases: private endpoints and IPs) stay hidden.
        for project in ("platform-registration", "platform-rehearsal-host"):
            self.assertIn(f"p, role:platform-viewer, applications, get, {project}/*, deny", rbac["policy.csv"])
        # orphanedResources (any value) lists every unmanaged object of a destination namespace in the anonymous tree.
        for template in (CHART / "templates").glob("*.yaml"):
            self.assertNotIn("orphanedResources", template.read_text(), template.name)
        # Every kind an Application ships must be tracked, or Argo skips it (ExcludedResourceWarning).
        inclusions = yaml.safe_load(cm["resource.inclusions"])
        tracked = {(g, k) for row in inclusions for g in row["apiGroups"] for k in row["kinds"]}
        for group, kinds in (("rbac.authorization.k8s.io", ("Role", "RoleBinding")), ("external-secrets.io", ("ExternalSecret",)),
                             ("networking.k8s.io", ("NetworkPolicy",)), ("", ("ConfigMap", "Service", "ServiceAccount"))):
            for k in kinds:
                self.assertIn((group, k), tracked)
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

    def test_development_values_add_the_observability_application_in_its_namespaces(self):
        docs = render("--values", str(ENV))
        app = kind(docs, "Application", "platform-observability")[0]["spec"]
        self.assertEqual(app["source"]["path"], "services/observability/deploy/platform-gitops")
        self.assertFalse(app["syncPolicy"]["automated"]["prune"])
        # Argo rejects an Application whose destination namespace is not in its project (an empty one included).
        allowed = {d["namespace"] for d in kind(docs, "AppProject", "platform-observability")[0]["spec"]["destinations"]}
        for name in ("platform-observability", "platform-public-entry"):
            self.assertIn(kind(docs, "Application", name)[0]["spec"]["destination"].get("namespace"),
                          allowed if name == "platform-observability" else {"platform-state"})
        project = kind(docs, "AppProject", "platform-observability")[0]["spec"]
        self.assertCountEqual([d["namespace"] for d in project["destinations"]],
                              ["bravo-observability", "platform-telemetry", "platform-state"])
        self.assertEqual(project["clusterResourceWhitelist"], [])
        cluster = kind(docs, "Secret", "argocd-cluster-platform")[0]["stringData"]
        self.assertEqual(cluster["namespaces"], "platform-state,bravo-observability,platform-telemetry")
        self.assertNotEqual(cluster.get("clusterResources"), "true")
        out = subprocess.run(["kubectl", "kustomize", str(ROOT / "services/observability/deploy/platform-gitops")], check=True,
                             capture_output=True, text=True, timeout=60)
        bundle = [d for d in yaml.safe_load_all(out.stdout) if d]
        self.assertFalse(kind(bundle, "Namespace") + kind(bundle, "Secret"))
        api = kind(bundle, "Deployment", "bravo-observability-api")[0]["spec"]["template"]["spec"]
        self.assertRegex(api["containers"][0]["image"], r"^ghcr\.io/softbank-hackathon-finals/platform-observability-api@sha256:[0-9a-f]{64}$")
        self.assertFalse(api["automountServiceAccountToken"])

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
        # A public Argo answers 403 for APIs that run server-side work for anyone with `applications, get`.
        argo = next(e for e in spec["entries"] if e["hostname"].startswith("platform-argocd."))
        if argo.get("public"):
            def denied(path):
                return any(re.match(d, path) for d in argo["deny_paths"])
            for path in ("/api/v1/applications/a/server-side-diff", "/api/v1/applications/a/manifests",
                         "/api/v1/applications/manifestsWithFiles", "/api/v1/applications/a/revisions/r/metadata",
                         "/application.ApplicationService/ServerSideDiff", "/application.ApplicationService/GetManifests"):
                self.assertTrue(denied(path), path)
            for path in ("/", "/applications", "/api/v1/applications", "/api/v1/applications/a",
                         "/api/v1/applications/a/resource-tree", "/api/v1/stream/applications", "/assets/favicon/favicon-32x32.png"):
                self.assertFalse(denied(path), path)


if __name__ == "__main__":
    unittest.main()
