"""Reject unsafe chart drift and preserve the target secret-delivery boundary."""
import copy
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("target_secrets", ROOT / "infra/envs/target-secrets/render.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


def cli(*args, objects=None):
    output = io.StringIO()
    with patch.object(render.sys, "stdin", io.StringIO(json.dumps(objects))), redirect_stdout(output):
        render.main(args)
    return json.loads(output.getvalue())


def chart_fixture():
    """Relevant shape of the pinned chart, including its unsafe default probe."""
    labels = {"app.kubernetes.io/name": "openbao", "app.kubernetes.io/instance": "target-openbao", "component": "server"}
    metadata = {"name": "target-openbao", "namespace": "bravo-secrets"}
    return [
        {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": metadata.copy(), "spec": {
            "selector": {"matchLabels": labels.copy()},
            "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": ""}}],
            "template": {"metadata": {"labels": labels.copy()}, "spec": {"containers": [{
                "name": "openbao", "image": render.BAO_IMAGE,
                "readinessProbe": {"exec": {"command": ["sh", "-ec", "bao status -tls-skip-verify"]},
                                   "failureThreshold": 3, "initialDelaySeconds": 5, "periodSeconds": 10,
                                   "successThreshold": 1, "timeoutSeconds": 10},
                "resources": {"requests": {"cpu": "250m", "memory": "128Mi"},
                              "limits": {"cpu": "1", "memory": "512Mi"}},
            }]}},
        }},
        {"apiVersion": "v1", "kind": "Service", "metadata": metadata.copy(), "spec": {"selector": labels.copy()}},
    ]


class ChartBoundaryTests(unittest.TestCase):
    def test_cli_replaces_chart_readiness_and_keeps_image_and_selectors(self):
        objects = chart_fixture()
        result = cli("finalize-bao", objects={"apiVersion": "v1", "kind": "List", "items": objects})
        original = copy.deepcopy(objects)
        original[0]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["exec"]["command"] = ["bao", "status", "-format=json"]
        self.assertEqual(result, {"apiVersion": "v1", "kind": "List", "items": original})
        self.assertNotIn("tls-skip-verify", json.dumps(result))
        self.assertEqual(render.bao_values()["server"]["extraEnvironmentVars"]["BAO_CACERT"], "/openbao/tls/ca.crt")

    def test_readiness_timing_is_rendered_and_enforced(self):
        probe = render.bao_values()["server"]["readinessProbe"]
        self.assertEqual(probe, {"enabled": True, "timeoutSeconds": 10, "periodSeconds": 10, "failureThreshold": 3})
        result = cli("finalize-bao", objects=chart_fixture())
        final = result["items"][0]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        self.assertEqual((final["timeoutSeconds"], final["periodSeconds"], final["failureThreshold"]), (10, 10, 3))
        self.assertEqual(final["exec"]["command"], ["bao", "status", "-format=json"])
        # Chart 0.30.6 defaults (rendered without bao-values) and a missing probe are refused.
        for change in ({"timeoutSeconds": 3, "periodSeconds": 5, "failureThreshold": 2}, {"timeoutSeconds": 3}, None):
            objects = chart_fixture()
            container = objects[0]["spec"]["template"]["spec"]["containers"][0]
            if change is None:
                del container["readinessProbe"]
            else:
                container["readinessProbe"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                cli("finalize-bao", objects=objects)

    def test_server_resources_are_rendered_and_enforced(self):
        self.assertEqual(render.bao_values()["server"]["resources"],
                         {"requests": {"cpu": "250m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}})
        # The earlier 50m/500m (Ready flapped on the target) and a missing block are refused.
        for change in ({"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
                       {"requests": {"cpu": "250m", "memory": "128Mi"}}, None):
            objects = chart_fixture()
            container = objects[0]["spec"]["template"]["spec"]["containers"][0]
            if change is None:
                del container["resources"]
            else:
                container["resources"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                cli("finalize-bao", objects=objects)

    def test_rejects_chart_namespace_image_selector_and_volume_drift(self):
        def mutations(objects):
            return [
                (objects[0]["metadata"], "namespace", "other-secrets"),
                (objects[0]["spec"]["template"]["spec"]["containers"][0], "image", "unreviewed:latest"),
                (objects[0]["spec"]["template"]["metadata"]["labels"], "component", "target"),
                (objects[1]["spec"]["selector"], "component", "target"),
                (objects[0]["spec"]["volumeClaimTemplates"][0]["spec"], "storageClassName", "default"),
            ]
        for index in range(5):
            objects = chart_fixture()
            container, key, value = mutations(objects)[index]
            container[key] = value
            with self.subTest(index=index), self.assertRaises(ValueError):
                cli("finalize-bao", objects=objects)

    def test_rejects_unexpected_chart_workloads_and_privilege_payloads(self):
        for kind in ("Secret", "Pod", "Job", "ClusterRoleBinding"):
            objects = chart_fixture() + [{"kind": kind, "metadata": {"name": "unreviewed"}}]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                cli("finalize-bao", objects=objects)
        for objects in ([], chart_fixture() + [chart_fixture()[0]], {"kind": "StatefulSet"}, ["invalid"]):
            with self.subTest(objects=objects), self.assertRaises(ValueError):
                cli("finalize-bao", objects=objects)


class DeliveryBoundaryTests(unittest.TestCase):
    def test_eso_namespace_and_network_have_separate_cli_outputs(self):
        namespace = cli("eso-namespace")
        self.assertEqual(namespace["kind"], "Namespace")
        self.assertEqual(namespace["metadata"]["name"], "external-secrets")
        self.assertNotIn("namespace", namespace["metadata"])
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        bao_peers = cli("network", "--api-ip", "10.96.0.1", "--api-endpoint-ip", "192.168.50.10")["spec"]["ingress"]
        self.assertEqual(len(bao_peers), 1)
        self.assertEqual(bao_peers[0]["ports"], [{"protocol": "TCP", "port": 8200}])
        self.assertEqual(bao_peers[0]["from"], [  # existing peers unchanged; writer peer is namespace AND label
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "external-secrets"}},
             "podSelector": {"matchLabels": {"app.kubernetes.io/name": "external-secrets"}}},
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "target-dev"}},
             "podSelector": {"matchLabels": {"bravo-secret-probe": "true"}}},
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "bravo-rehearsal-control"}},
             "podSelector": {"matchLabels": {"app.kubernetes.io/component": "rehearsal-openbao-writer"}}},
            # attempt namespace label AND the T15 candidate / verifier Pod label, in one peer each
            {"namespaceSelector": {"matchLabels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}},
             "podSelector": {"matchLabels": {"app": "rehearsal-app"}}},
            {"namespaceSelector": {"matchLabels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}},
             "podSelector": {"matchLabels": {"role": "credential-verifier"}}},
        ])
        writer_namespace = render.rehearsal_writer_identity()["items"][0]
        self.assertNotEqual(writer_namespace["metadata"]["labels"].get("app.kubernetes.io/part-of"), "bravo-rehearsal")
        for stage, expected in (("network", "bravo-secrets"), ("eso-network", "external-secrets")):
            policy = cli(stage, "--api-ip", "10.96.0.1", "--api-endpoint-ip", "192.168.50.10")
            with self.subTest(stage=stage):
                self.assertEqual(policy["kind"], "NetworkPolicy")
                self.assertEqual(policy["metadata"]["namespace"], expected)
                self.assertEqual(set(policy["spec"]["policyTypes"]), {"Ingress", "Egress"})
                ip_paths = [(peer["ipBlock"]["cidr"], rule["ports"]) for rule in policy["spec"]["egress"]
                            for peer in rule["to"] if "ipBlock" in peer]
                self.assertEqual(ip_paths, [("10.96.0.1/32", [{"protocol": "TCP", "port": 443}]),
                                            ("192.168.50.10/32", [{"protocol": "TCP", "port": 6443}])])

    def test_existing_database_secret_is_not_adopted_and_values_are_not_rendered(self):
        items = cli("delivery")["items"]
        self.assertFalse(any(item["kind"] == "Secret" for item in items))
        targets = {item["spec"]["target"]["name"] for item in items if item["kind"] == "ExternalSecret"}
        self.assertEqual(targets, {"target-app-credentials", "target-app-objects", "target-app-admin"})
        self.assertNotIn("target-app-db", json.dumps(items))
        store = next(item for item in items if item["kind"] == "SecretStore")["spec"]["provider"]["vault"]
        self.assertTrue(store["server"].startswith("https://"))
        self.assertEqual(store["caProvider"], {"type": "ConfigMap", "name": "target-openbao-ca", "key": "ca.crt"})
        self.assertEqual(store["auth"]["kubernetes"]["serviceAccountRef"], {"name": "target-eso-reader", "audiences": ["openbao"]})
        account = next(item for item in items if item["kind"] == "ServiceAccount")
        self.assertFalse(account["automountServiceAccountToken"])

    def test_retained_volume_binds_to_only_the_named_node_and_claim(self):
        items = cli("foundation", "--node-name", "fixture-node")["items"]
        volume = next(item for item in items if item["kind"] == "PersistentVolume")["spec"]
        claim = next(item for item in items if item["kind"] == "PersistentVolumeClaim")
        self.assertEqual(volume["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(volume["claimRef"], {"namespace": claim["metadata"]["namespace"], "name": claim["metadata"]["name"]})
        self.assertEqual(claim["spec"]["volumeName"], "bravo-target-openbao-data")
        self.assertEqual(volume["storageClassName"], claim["spec"]["storageClassName"])
        self.assertEqual(volume["storageClassName"], "")
        expression = volume["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]
        self.assertEqual(expression, {"key": "kubernetes.io/hostname", "operator": "In", "values": ["fixture-node"]})


if __name__ == "__main__":
    unittest.main()
