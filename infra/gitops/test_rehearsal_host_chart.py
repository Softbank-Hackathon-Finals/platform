# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""T15 rehearsal host: one writer, read-only inputs, token-only identity, distinct GitOps project."""
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import bootstrap
import yaml
from bootstrap import verify_rehearsal_host
from test_registration_chart import ArgoBoundary

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/charts/rehearsal-host"
RUNTIME = {
    "configuration": {"existingConfigMap": "host-config", "sha256": "c" * 64},
    "reader": {"existingConfigMap": "reader-config", "catalogClaim": "existing-catalog"},
    "recordsClaim": "t15-records",
    "secrets": {"catalogKey": "existing-key", "host": "t15-host"},
}
VALUES = {"image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40, **RUNTIME}
WRITABLE = {"records", "staging", "tmp"}


def render(values, namespace="platform-state"):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "values.json"
        file.write_text(json.dumps(values))
        return subprocess.run(["helm", "template", "rehearsal-host", str(CHART),
            "--namespace", namespace, "--kube-version", "1.35.9", "-f", str(file)],
            check=False, capture_output=True, text=True, timeout=30)


class HostChart(unittest.TestCase):
    def test_single_writer_with_read_only_inputs_and_token_only_identity(self):
        result = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertCountEqual([d["kind"] for d in docs], ["ServiceAccount", "Service", "Deployment"])
        sa = next(d for d in docs if d["kind"] == "ServiceAccount")
        self.assertFalse(sa["automountServiceAccountToken"])
        dep = next(d for d in docs if d["kind"] == "Deployment")
        self.assertEqual((dep["spec"]["replicas"], dep["spec"]["strategy"]["type"]), (1, "Recreate"))
        pod = dep["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "t15-rehearsal-host")
        self.assertFalse(pod["automountServiceAccountToken"])
        labels = dep["spec"]["template"]["metadata"]["labels"]
        self.assertEqual((labels["bravo.dev/db-client"], labels["bravo.dev/target-api-client"]), ("runtime", "rehearsal"))
        c, = pod["containers"]
        self.assertIn("bravo_platform_events.credential_host:create_ports", c["args"])
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        for mount in c["volumeMounts"]:
            self.assertEqual(mount.get("readOnly", False), mount["name"] not in WRITABLE, mount["name"])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["catalog"]["persistentVolumeClaim"], {"claimName": "existing-catalog", "readOnly": True})
        self.assertEqual(volumes["records"]["persistentVolumeClaim"], {"claimName": "t15-records"})
        token, = volumes["intake-token"]["projected"]["sources"]
        self.assertEqual(token["serviceAccountToken"],
                         {"audience": "bravo-t10-rehearsal-intake", "expirationSeconds": 600, "path": "token"})
        paths = {i["path"] for i in volumes["config"]["configMap"]["items"]}
        self.assertTrue({"host.json", "ports.json", "access/manager.kubeconfig", "access/runner.kubeconfig"} <= paths)

    def test_denies_unreviewed_image_namespace_and_added_inputs(self):
        cases = [({}, "platform-state"), (VALUES, "other")]
        for path, value in ((["image", "repository"], "example.invalid/foreign"),
                            (["image", "digest"], "latest"),
                            (["recordsClaim"], "../../other"),
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

    def test_gitops_project_is_distinct_and_names_only_the_host(self):
        result = ArgoBoundary().render({"rehearsalHost": {"enabled": True, "runtime": deepcopy(RUNTIME)}})
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        app = next(d for d in docs if d["kind"] == "Application" and d["metadata"]["name"] == "platform-rehearsal-host")
        project = next(d for d in docs if d["kind"] == "AppProject" and d["metadata"]["name"] == "platform-rehearsal-host")
        self.assertEqual(app["spec"]["source"]["path"], "infra/charts/rehearsal-host")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "platform-gitops-rehearsal-host")
        self.assertTrue(all(rule["resourceNames"] == ["t15-rehearsal-host"] for rule in role["rules"]))
        policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
        self.assertIn("t15-rehearsal-host", json.dumps(policy["spec"]["validations"]))
        disabled = ArgoBoundary().render({})
        self.assertNotIn("t15-rehearsal-host", disabled.stdout)
        bad = deepcopy(RUNTIME) | {"image": {"digest": "sha256:" + "e" * 64}}
        self.assertNotEqual(ArgoBoundary().render({"rehearsalHost": {"enabled": True, "runtime": bad}}).returncode, 0)

    def test_bootstrap_requires_existing_inputs_and_published_image(self):
        seen = []
        def get(kind, name, namespace):
            self.assertEqual(namespace, "platform-state")
            seen.append((kind, name))
            return {"metadata": {"name": name}}
        # Exercise both pin states without depending on the repository's current pin.
        pins = {"": True, "sha256:" + "d" * 64: False}
        for digest, rejected in pins.items():
            with self.subTest(digest=digest), tempfile.TemporaryDirectory() as directory:
                pin = Path(directory) / "infra/gitops/environments/development/rehearsal-host-image.json"
                pin.parent.mkdir(parents=True)
                pin.write_text(json.dumps({"image": {"digest": digest}}))
                seen.clear()
                with patch.object(bootstrap, "ROOT", Path(directory)):
                    if rejected:
                        with self.assertRaises(ValueError):
                            verify_rehearsal_host(get, {"enabled": True, "runtime": deepcopy(RUNTIME)})
                    else:
                        verify_rehearsal_host(get, {"enabled": True, "runtime": deepcopy(RUNTIME)})
        self.assertCountEqual(seen, [("configmap", "host-config"), ("configmap", "reader-config"),
                                     ("pvc", "existing-catalog"), ("pvc", "t15-records"),
                                     ("secret", "existing-key"), ("secret", "t15-host")])
        for value in ({"enabled": False, "runtime": RUNTIME},
                      {"enabled": True, "runtime": RUNTIME | {"image": {}}},
                      {"enabled": True, "runtime": {k: v for k, v in RUNTIME.items() if k != "recordsClaim"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_rehearsal_host(get, value)


if __name__ == "__main__":
    if sys.argv[1:] == ["--render"]:
        result = render(VALUES)
        if result.returncode:
            raise SystemExit(result.stderr)
        print(result.stdout)
    else:
        unittest.main()
