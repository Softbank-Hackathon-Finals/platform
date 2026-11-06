# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Rendered Reader adoption boundary, including explicit invalid deployment inputs."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml
from bootstrap import verify_reader
from test_registration_chart import ArgoBoundary

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/charts/product-reader"
VALUES = {
    "image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40,
    "configuration": {"existingConfigMap": "reader-config", "sha256": "c" * 64},
    "catalogClaim": "existing-catalog",
    "secrets": {"catalogKey": "existing-key", "tls": "existing-tls"},
}

def render(values, namespace="platform-state"):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "values.json"
        file.write_text(json.dumps(values))
        return subprocess.run(["helm", "template", "product-reader", str(CHART),
            "--namespace", namespace, "--kube-version", "1.35.9", "-f", str(file)],
            check=False, capture_output=True, text=True, timeout=30)

class ReaderChart(unittest.TestCase):
    def test_adopts_only_workload_and_preserves_external_storage(self):
        result = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertCountEqual([d["kind"] for d in docs], ["Service", "Deployment"])
        dep = next(d for d in docs if d["kind"] == "Deployment")
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "platform-runtime")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("initContainers", pod)
        c, = pod["containers"]
        self.assertIn("--initial-proposal", c["args"])
        self.assertEqual(c["image"], "ghcr.io/softbank-hackathon-finals/platform-product-reader@" + VALUES["image"]["digest"])
        self.assertTrue(all(m["readOnly"] for m in c["volumeMounts"]))
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["catalog"]["persistentVolumeClaim"],
                         {"claimName": "existing-catalog", "readOnly": True})
        self.assertEqual(volumes["catalog-key"]["secret"]["secretName"], "existing-key")
        self.assertEqual(volumes["tls"]["secret"]["secretName"], "existing-tls")
        self.assertEqual(dep["spec"]["selector"]["matchLabels"], {
            "app.kubernetes.io/name": "t10-product-reader", "component": "platform"})

    def test_denies_unreviewed_image_namespace_and_added_authority(self):
        cases = [({}, "platform-state"), (VALUES, "other")]
        for path, value in ((["image", "repository"], "example.invalid/foreign"),
                            (["image", "digest"], "latest"),
                            (["catalogClaim"], "../../other"),
                            (["serviceAccountName"], "admin"),
                            (["extraVolumes"], [{"hostPath": {"path": "/"}}])):
            candidate = deepcopy(VALUES)
            parent = candidate
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            cases.append((candidate, "platform-state"))
        for values, namespace in cases:
            with self.subTest(values=values, namespace=namespace):
                self.assertNotEqual(render(values, namespace).returncode, 0)

    def test_reader_project_is_distinct_and_has_no_data_or_identity_writes(self):
        runtime = {k: VALUES[k] for k in ("configuration", "secrets", "catalogClaim")}
        result = ArgoBoundary().render({"productReader": {"enabled": True, "runtime": runtime}})
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        app = next(d for d in docs if d["kind"] == "Application")
        project = next(d for d in docs if d["kind"] == "AppProject")
        self.assertEqual(app["spec"]["project"], "platform-product-reader")
        self.assertEqual(project["spec"]["namespaceResourceWhitelist"],
                         [{"group": "apps", "kind": "Deployment"}, {"group": "", "kind": "Service"}])
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertEqual(app["spec"]["source"]["path"], "infra/charts/product-reader")
        role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "platform-gitops-product-reader")
        self.assertEqual({r for rule in role["rules"] for r in rule["resources"]}, {"deployments", "services"})
        self.assertTrue(all(rule["resourceNames"] == ["t10-product-reader"] for rule in role["rules"]))
        runtime["image"] = {"digest": "sha256:" + "e" * 64}
        self.assertNotEqual(ArgoBoundary().render({"productReader": {"enabled": True, "runtime": runtime}}).returncode, 0)

class ReaderAdoption(unittest.TestCase):
    def test_uid_configuration_and_mount_changes_are_not_adopted(self):
        runtime = {"configuration": {"existingConfigMap": "reader-config"},
                   "catalogClaim": "existing-catalog", "secrets": {
                       "database": "platform-runtime-db", "databaseCA": "platform-db-ca",
                       "catalogKey": "existing-key", "tls": "existing-tls", "registry": "platform-ghcr-reader"}}
        data = {"reader.json": "{}", "github-jwks.json": "{}"}
        runtime["configuration"]["sha256"] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        value = VALUES | runtime
        result = render(value)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        objects = {(d["kind"].lower(), d["metadata"]["name"]): d for d in docs}
        for kind, name in [("serviceaccount", "platform-runtime"), ("pvc", "existing-catalog"), ("configmap", "reader-config")]:
            objects[(kind, name)] = {"metadata": {"name": name}}
        objects[("configmap", "reader-config")]["data"] = data
        for name in runtime["secrets"].values():
            objects[("secret", name)] = {"metadata": {"name": name}}
        for (kind, name), obj in objects.items():
            obj["metadata"]["uid"] = "fixture-" + kind + "-" + name
        expected = {
            "deployment_uid": objects[("deployment", "t10-product-reader")]["metadata"]["uid"],
            "service_uid": objects[("service", "t10-product-reader")]["metadata"]["uid"],
            "service_account_uid": objects[("serviceaccount", "platform-runtime")]["metadata"]["uid"],
            "config_uid": objects[("configmap", "reader-config")]["metadata"]["uid"],
            "catalog_uid": objects[("pvc", "existing-catalog")]["metadata"]["uid"],
            "secret_uids": {name: objects[("secret", name)]["metadata"]["uid"] for name in runtime["secrets"].values()},
        }
        def get(kind, name, namespace):
            self.assertEqual(namespace, "platform-state")
            return current[(kind, name)]
        current = deepcopy(objects)
        verify_reader(get, runtime, expected)
        for fault in ["uid", "config", "selector", "catalog-write", "mount", "other-owner"]:
            with self.subTest(fault=fault):
                current = deepcopy(objects)
                dep = current[("deployment", "t10-product-reader")]
                if fault == "uid":
                    current[("service", "t10-product-reader")]["metadata"]["uid"] = "replaced-service"
                elif fault == "config":
                    current[("configmap", "reader-config")]["data"]["reader.json"] = "changed"
                elif fault == "selector":
                    dep["spec"]["selector"]["matchLabels"]["component"] = "other"
                elif fault == "catalog-write":
                    next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "catalog")["persistentVolumeClaim"]["readOnly"] = False
                elif fault == "mount":
                    next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "tls")["secret"]["secretName"] = "other-tls"
                else:
                    dep["metadata"]["annotations"] = {"argocd.argoproj.io/tracking-id": "another-app:apps/Deployment:platform-state/t10-product-reader"}
                with self.assertRaises(ValueError):
                    verify_reader(get, runtime, expected)


if __name__ == "__main__":
    if sys.argv[1:] == ["--render"]:
        result = render(VALUES)
        if result.returncode:
            raise SystemExit(result.stderr)
        print(result.stdout)
    else:
        unittest.main()
