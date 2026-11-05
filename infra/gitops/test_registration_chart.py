# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Exercise the rendered workload and reject unsafe deployment inputs."""

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1] / "charts/registration"
VALUES = {"image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40,
          "configuration": {"sha256": "c" * 64}}


def render(values, namespace="platform-state"):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "values.json"
        file.write_text(json.dumps(values))
        return subprocess.run(["helm", "template", "registration", str(CHART), "--namespace", namespace,
                               "--kube-version", "1.35.9", "--values", str(file)],
                              check=False, capture_output=True, text=True, timeout=30)


class RegistrationChart(unittest.TestCase):
    def test_preserves_identity_and_secret_boundaries(self):
        result = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = list(yaml.safe_load_all(result.stdout))
        self.assertCountEqual([d["kind"] for d in docs], ["ServiceAccount", "Service", "Deployment"])
        self.assertTrue(all(d["metadata"]["name"] == "t10-registration" for d in docs))
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["imagePullSecrets"], [{"name": "platform-ghcr-reader"}])
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        init = pod["initContainers"][0]
        self.assertEqual({v["name"] for v in init["volumeMounts"]}, {"checkout", "checkout-catalog"})
        for container in pod["containers"] + pod["initContainers"]:
            self.assertEqual(container["imagePullPolicy"], "Always")
            self.assertEqual(container["image"].split("@")[1], VALUES["image"]["digest"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertFalse(any(v.get("persistentVolumeClaim") or v.get("hostPath") for v in pod["volumes"]))

    def test_rejects_mutable_foreign_or_injected_inputs(self):
        bad = [({}, "platform-state"), (VALUES, "other-tenant")]
        for key, value in (("digest", "latest"), ("repository", "example.invalid/unreviewed")):
            values = deepcopy(VALUES); values["image"][key] = value; bad.append((values, "platform-state"))
        for value in (None, "../../other", "$(touch marker)"):
            values = deepcopy(VALUES); values["configuration"]["existingConfigMap"] = value
            bad.append((values, "platform-state"))
        values = deepcopy(VALUES); values["extraVolumes"] = [{"hostPath": {"path": "/"}}]
        bad.append((values, "platform-state"))
        for values, namespace in bad:
            with self.subTest(values=values, namespace=namespace):
                self.assertNotEqual(render(values, namespace).returncode, 0)

    def test_configuration_change_changes_pod_template(self):
        old = list(yaml.safe_load_all(render(VALUES).stdout))
        changed = deepcopy(VALUES); changed["configuration"]["sha256"] = "d" * 64
        new = list(yaml.safe_load_all(render(changed).stdout))
        before = next(d for d in old if d["kind"] == "Deployment")["spec"]["template"]
        after = next(d for d in new if d["kind"] == "Deployment")["spec"]["template"]
        self.assertNotEqual(before, after)
        self.assertEqual(before["spec"], after["spec"])


if __name__ == "__main__":
    unittest.main()
