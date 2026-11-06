# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Exercise the rendered workload and reject unsafe deployment inputs."""

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "charts/registration"
VALUES = {
    "image": {"digest": "sha256:" + "a" * 64},
    "sourceRevision": "b" * 40,
    "configuration": {"sha256": "c" * 64},
}


def render(values, namespace="platform-state"):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "values.json"
        file.write_text(json.dumps(values))
        return subprocess.run(
            [
                "helm",
                "template",
                "registration",
                str(CHART),
                "--namespace",
                namespace,
                "--kube-version",
                "1.35.9",
                "--values",
                str(file),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


class RegistrationChart(unittest.TestCase):
    def test_preserves_identity_and_secret_boundaries(self):
        result = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertCountEqual(
            [d["kind"] for d in docs], ["ServiceAccount", "Service", "Deployment"]
        )
        self.assertTrue(all(d["metadata"]["name"] == "t10-registration" for d in docs))
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["imagePullSecrets"], [{"name": "platform-ghcr-reader"}])
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        init = pod["initContainers"][0]
        self.assertEqual(
            {v["name"] for v in init["volumeMounts"]}, {"checkout", "checkout-catalog"}
        )
        for container in pod["containers"] + pod["initContainers"]:
            self.assertEqual(container["imagePullPolicy"], "Always")
            self.assertEqual(
                container["image"].split("@")[1], VALUES["image"]["digest"]
            )
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertFalse(
            any(
                v.get("persistentVolumeClaim") or v.get("hostPath")
                for v in pod["volumes"]
            )
        )
        # Same mount as deploy/registration/render.py, so gate-grants.json reaches the host.
        registration = next(v for v in pod["volumes"] if v["name"] == "registration")
        self.assertEqual(set(registration["configMap"]), {"name", "defaultMode"})

    def test_rejects_mutable_foreign_or_injected_inputs(self):
        bad = [({}, "platform-state"), (VALUES, "other-tenant")]
        for key, value in (
            ("digest", "latest"),
            ("repository", "example.invalid/unreviewed"),
        ):
            values = deepcopy(VALUES)
            values["image"][key] = value
            bad.append((values, "platform-state"))
        for value in (None, "../../other", "$(touch marker)"):
            values = deepcopy(VALUES)
            values["configuration"]["existingConfigMap"] = value
            bad.append((values, "platform-state"))
        values = deepcopy(VALUES)
        values["extraVolumes"] = [{"hostPath": {"path": "/"}}]
        bad.append((values, "platform-state"))
        for values, namespace in bad:
            with self.subTest(values=values, namespace=namespace):
                self.assertNotEqual(render(values, namespace).returncode, 0)

    def test_configuration_change_changes_pod_template(self):
        old = list(yaml.safe_load_all(render(VALUES).stdout))
        changed = deepcopy(VALUES)
        changed["configuration"]["sha256"] = "d" * 64
        new = list(yaml.safe_load_all(render(changed).stdout))
        before = next(d for d in old if d["kind"] == "Deployment")["spec"]["template"]
        after = next(d for d in new if d["kind"] == "Deployment")["spec"]["template"]
        self.assertNotEqual(before, after)
        self.assertEqual(before["spec"], after["spec"])


class ArgoBoundary(unittest.TestCase):
    def render(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(value))
            return subprocess.run(
                [
                    "helm",
                    "template",
                    "bravo-gitops",
                    str(CHART.parents[1] / "gitops/argocd"),
                    "--namespace",
                    "bravo-gitops",
                    "--kube-version",
                    "1.35.9",
                    "-f",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_private_runtime_cannot_override_published_image(self):
        value = {
            "registration": {
                "enabled": True,
                "runtime": {
                    "configuration": VALUES["configuration"],
                    "image": {"repository": "example.invalid/unreviewed"},
                },
            }
        }
        self.assertNotEqual(self.render(value).returncode, 0)

    def test_core_has_no_cluster_roles_or_public_server(self):
        result = self.render({})
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        # A fresh cluster cannot map custom resources until the first release
        # has installed and established Argo's CRDs.
        self.assertFalse(any(d["apiVersion"].startswith("argoproj.io/") for d in docs))
        self.assertTrue(
            {"applications.argoproj.io", "appprojects.argoproj.io"}.issubset(
                {d["metadata"]["name"] for d in docs if d["kind"] == "CustomResourceDefinition"}
            )
        )
        self.assertFalse(
            any(
                d["kind"] in ("ClusterRole", "ClusterRoleBinding", "Application")
                for d in docs
            )
        )
        workloads = [d for d in docs if d["kind"] in ("Deployment", "StatefulSet")]
        self.assertFalse(
            any(
                d["metadata"]["name"].endswith("-server")
                and not d["metadata"]["name"].endswith("-repo-server")
                for d in workloads
            )
        )
        bindings = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"]
        self.assertEqual(bindings[0]["spec"]["validationActions"], ["Deny"])

    def test_application_reads_git_digest_and_preserves_data(self):
        result = self.render(
            {
                "registration": {
                    "enabled": True,
                    "runtime": {"configuration": VALUES["configuration"]},
                }
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        app = next(d for d in docs if d["kind"] == "Application")
        project = next(d for d in docs if d["kind"] == "AppProject")
        self.assertEqual(project["metadata"]["name"], app["spec"]["project"])
        self.assertEqual(app["spec"]["source"]["targetRevision"], "develop")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["selfHeal"])
        self.assertNotIn("image", app["spec"]["source"]["helm"]["valuesObject"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--render"]:
        result = render(VALUES)
        if result.returncode:
            raise SystemExit(result.stderr)
        print(result.stdout)
    else:
        unittest.main()
