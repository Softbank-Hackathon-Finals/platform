"""Chart boundary: no secrets in values, no journal replacement, one write owner."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import test_registration_chart as registration_chart
import yaml
from bootstrap import verify_edge

CHART = Path(__file__).resolve().parents[1] / "charts/edge-control"
VALUES = {"image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40,
          "configuration": {"sha256": "c" * 64}, "ports": "deployment_ports:create_ports"}


def render(values):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.json"
        path.write_text(json.dumps(values))
        return subprocess.run(["helm", "template", "edge-control", str(CHART),
                               "--namespace", "platform-state", "-f", str(path)],
                              check=False, text=True, capture_output=True, timeout=30)


class ChartBoundary(unittest.TestCase):
    def test_journal_uses_retained_explicit_platform_local_volume(self):
        result = subprocess.run(["helm", "template", "edge-journal", str(CHART.parent / "edge-journal"),
                                 "--namespace", "platform-state", "--set", "storageClassName=platform-local,nodeName=verified-node"],
                                check=False, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        volume, claim = list(yaml.safe_load_all(result.stdout))
        self.assertEqual(volume["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(volume["spec"]["local"]["path"], "/var/lib/bravo-platform/edge-journal")
        self.assertEqual(volume["spec"]["claimRef"], {"namespace": "platform-state", "name": claim["metadata"]["name"]})
        self.assertEqual(claim["spec"]["volumeName"], volume["metadata"]["name"])
        self.assertTrue(all(o["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep" for o in (volume, claim)))

    def test_gitops_only_writes_named_controller_and_rejects_image_override(self):
        runtime = {"configuration": {"existingConfigMap": "edge-runtime", "sha256": "c" * 64},
                   "secrets": {}, "journalClaim": "edge-journal", "ports": "deployment_ports:create_ports"}
        result = registration_chart.ArgoBoundary().render({"edgeControl": {"enabled": True, "runtime": runtime}})
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = list(yaml.safe_load_all(result.stdout))
        app = next(o for o in objects if o["kind"] == "Application")
        self.assertEqual(app["spec"]["source"]["path"], "infra/charts/edge-control")
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        role = next(o for o in objects if o["kind"] == "Role" and o["metadata"]["name"] == "platform-gitops-edge-control")
        self.assertTrue(all(rule["resourceNames"] == ["t17-edge-control"] for rule in role["rules"]))
        self.assertEqual({r for rule in role["rules"] for r in rule["resources"]}, {"deployments", "serviceaccounts"})
        runtime["image"] = {"digest": "sha256:" + "f" * 64}
        self.assertNotEqual(registration_chart.ArgoBoundary().render({"edgeControl": {"enabled": True, "runtime": runtime}}).returncode, 0)

    def test_one_controller_retained_external_journal_and_no_cluster_permissions(self):
        result = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = list(yaml.safe_load_all(result.stdout))
        self.assertEqual({obj["kind"] for obj in objects}, {"ServiceAccount", "Deployment"})
        deployment = next(o for o in objects if o["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["journal"]["persistentVolumeClaim"]["claimName"], "t17-edge-journal")
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn("ports", container)
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        self.assertTrue(all(mounts[n]["readOnly"] for n in ("credential", "ca", "provider", "authority", "runtime")))

    def test_missing_identity_digest_or_factory_does_not_render(self):
        for key in ("image", "sourceRevision", "configuration", "ports"):
            values = {k: v for k, v in VALUES.items() if k != key}
            with self.subTest(key=key):
                self.assertNotEqual(render(values).returncode, 0)

    def test_secret_values_replica_and_command_overrides_are_rejected(self):
        for extra in ({"replicas": 2}, {"token": "synthetic"}, {"command": ["sh"]},
                      {"secrets": {"providerToken": "synthetic"}}):
            self.assertNotEqual(render(VALUES | extra).returncode, 0)


class Adoption(unittest.TestCase):
    def test_replaced_input_unretained_journal_and_unowned_worker_are_rejected(self):
        data = {"edge.json": json.dumps({"journal": "/journal/edge.db", "token_file": "/provider/token"})}
        runtime = {"configuration": {"existingConfigMap": "edge-runtime",
                    "sha256": hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()},
                   "secrets": {"provider": "provider-secret"}, "journalClaim": "edge-journal",
                   "ports": "deployment_ports:create_ports"}
        objects = {("namespace", "platform-state"): {"metadata": {"uid": "namespace"}},
                   ("configmap", "edge-runtime"): {"metadata": {"uid": "config"}, "data": data},
                   ("pvc", "edge-journal"): {"metadata": {"uid": "journal"},
                        "status": {"phase": "Bound"}, "spec": {"accessModes": ["ReadWriteOnce"]}},
                   ("secret", "provider-secret"): {"metadata": {"uid": "secret"}}}
        expected = {"namespace_uid": "namespace", "config_uid": "config", "journal_uid": "journal",
                    "secret_uids": {"provider-secret": "secret"}, "deployment_uid": None, "service_account_uid": None}
        def get(kind, name, namespace):
            self.assertEqual(namespace, "platform-state")
            return current.get((kind, name))
        current = deepcopy(objects)
        verify_edge(get, runtime, expected)
        for fault in ("namespace", "config", "journal", "secret", "pending", "multiwriter", "existing"):
            with self.subTest(fault=fault):
                current = deepcopy(objects)
                if fault in {"namespace", "config", "journal", "secret"}:
                    next(o for o in current.values() if o["metadata"]["uid"] == fault)["metadata"]["uid"] = "changed"
                elif fault == "pending":
                    current[("pvc", "edge-journal")]["status"]["phase"] = "Pending"
                elif fault == "multiwriter":
                    current[("pvc", "edge-journal")]["spec"]["accessModes"] = ["ReadWriteMany"]
                else:
                    current[("deployment", "t17-edge-control")] = {"metadata": {"uid": "existing"}}
                with self.assertRaises(ValueError):
                    verify_edge(get, runtime, expected)


if __name__ == "__main__":
    unittest.main()
