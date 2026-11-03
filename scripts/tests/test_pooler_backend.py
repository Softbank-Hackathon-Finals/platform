"""Source pooler route isolation, exact target selection and CLI contract."""
import copy
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pooler_backend", ROOT / "infra/envs/executor-network/pooler_backend.py")
render = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(ROOT / "infra/envs/executor-network"), *sys.path]):
    SPEC.loader.exec_module(render)


def observed_services():
    return {"apiVersion": "v1", "kind": "List", "items": [{
        "apiVersion": "v1", "kind": "Service", "metadata": {
            "name": "ts-fixture-pooler", "namespace": "tailscale-system", "resourceVersion": "100",
            "uid": "00000000-0000-4000-8000-abcdefabcdef", "labels": {
                "tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "svc",
                "tailscale.com/parent-resource": "target-db-for-pooler",
                "tailscale.com/parent-resource-ns": "source-dev",
                "tailscale.com/proxy-group": "bravo-source-pooler", "tailscale.com/svc-type": "egress",
            }}, "spec": {"type": "ClusterIP", "ports": [
                {"name": "postgres", "port": 5432, "targetPort": 10300, "protocol": "TCP"},
                {"name": "tailscale-health-check", "port": 9002, "targetPort": 9002, "protocol": "TCP"},
            ]},
    }]}


def permits(policy, namespace, labels, protocol, port):
    for rule in policy["spec"]["ingress"]:
        if not any(p["protocol"] == protocol and p["port"] == port for p in rule["ports"]):
            continue
        if "from" not in rule:
            return True
        for peer in rule["from"]:
            if (peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": namespace}
                    and all(labels.get(k) == v for k, v in peer["podSelector"]["matchLabels"].items())):
                return True
    return False


class SourcePoolerBackendTests(unittest.TestCase):
    def test_only_new_source_pooler_identity_and_target_db_service_are_rendered(self):
        items = render.egress("bravo-target-db.example.ts.net")
        self.assertEqual(len(items), 2)
        group, service = items
        self.assertEqual(group, {
            "apiVersion": "tailscale.com/v1alpha1", "kind": "ProxyGroup",
            "metadata": {"name": "bravo-source-pooler", "labels": {"component": "source"}},
            "spec": {"type": "egress", "replicas": 1, "tags": ["tag:bravo-source-pooler"]},
        })
        self.assertEqual(service["apiVersion"], "v1")
        self.assertEqual(service["kind"], "Service")
        self.assertEqual(service["metadata"]["name"], "target-db-for-pooler")
        self.assertEqual(service["metadata"]["namespace"], "source-dev")
        self.assertEqual(service["metadata"]["labels"]["component"], "source")
        self.assertEqual(service["metadata"]["annotations"], {
            "tailscale.com/tailnet-fqdn": "bravo-target-db.example.ts.net",
            "tailscale.com/proxy-group": "bravo-source-pooler",
        })
        self.assertEqual(service["spec"], {
            "type": "ExternalName", "externalName": "placeholder",
            "ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}],
        })

    def test_rejects_other_destinations_addresses_or_noncanonical_fqdns(self):
        for fqdn in (None, "", 5432, "bravo-source-db.example.ts.net",
                     "bravo-target-control-pooler.example.ts.net", "bravo-target-operator.example.ts.net",
                     "bravo-target-db.example.ts.net.evil.invalid", "bravo-target-db.example.ts.net.",
                     "https://bravo-target-db.example.ts.net", "bravo-target-db.example.ts.net:5432",
                     "bravo-target-db.-example.ts.net", "bravo-target-db.example-.ts.net",
                     "bravo-target-db.example_team.ts.net", "bravo-target-db.Example.ts.net",
                     "bravo-target-db.sub.example.ts.net", "100.100.100.100",
                     "bravo-target-db." + "a" * 64 + ".ts.net"):
            with self.subTest(fqdn=fqdn), self.assertRaises(ValueError):
                render.egress(fqdn)

    def test_dns_label_boundaries_and_output_are_deterministic(self):
        for suffix in ("a", "example-tailnet", "a" * 63):
            fqdn = f"bravo-target-db.{suffix}.ts.net"
            with self.subTest(suffix=suffix):
                result = render.egress(fqdn)
                self.assertEqual(result[1]["metadata"]["annotations"]["tailscale.com/tailnet-fqdn"], fqdn)
                self.assertEqual(result, render.egress(fqdn))

    def test_cli_requires_an_explicit_target_and_renders_a_kubernetes_list(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            render.main([])
        self.assertEqual(error.exception.code, 2)
        output = io.StringIO()
        with redirect_stdout(output):
            render.main(["--target-db-fqdn", "bravo-target-db.example.ts.net"])
        self.assertEqual(json.loads(output.getvalue()), {
            "apiVersion": "v1", "kind": "List", "items": render.egress("bravo-target-db.example.ts.net"),
        })
        process = subprocess.run([sys.executable, SPEC.origin, "routes", "--target-db-fqdn",
                                  "bravo-target-db.example.ts.net"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout), json.loads(output.getvalue()))


class SourcePoolerIngressTests(unittest.TestCase):
    def setUp(self):
        self.policy = render.proxy_ingress(observed_services(), transport_ports=[45000, 46000])

    def test_selects_only_this_group_and_separates_pooler_health_and_transport(self):
        self.assertEqual(self.policy["metadata"]["namespace"], "tailscale-system")
        self.assertEqual(self.policy["metadata"]["name"], "source-pooler-backend-ingress")
        self.assertEqual(self.policy["metadata"]["labels"]["component"], "source")
        self.assertEqual(self.policy["spec"]["podSelector"], {"matchLabels": {
            "tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "proxygroup",
            "tailscale.com/parent-resource": "bravo-source-pooler",
        }})
        self.assertEqual(self.policy["spec"]["policyTypes"], ["Ingress"])
        self.assertNotIn("egress", self.policy["spec"])
        for port in (5432, 10300):
            self.assertTrue(permits(self.policy, "source-dev", {"app": "source-pgbouncer"}, "TCP", port))
            for ns, labels in (("target-dev", {"app": "source-pgbouncer"}),
                               ("source-dev", {"app": "target-pgbouncer"}),
                               ("source-dev", {"app": "collector"}),
                               ("tailscale-system", {"app": "operator"})):
                self.assertFalse(permits(self.policy, ns, labels, "TCP", port))
        self.assertTrue(permits(self.policy, "tailscale-system", {"app": "operator"}, "TCP", 9002))
        for ns, labels in (("source-dev", {"app": "operator"}), ("tailscale-system", {"app": "other"}),
                           ("source-dev", {"app": "source-pgbouncer"})):
            self.assertFalse(permits(self.policy, ns, labels, "TCP", 9002))
        for port in (45000, 46000):
            self.assertTrue(permits(self.policy, "anywhere", {}, "UDP", port))
            self.assertFalse(permits(self.policy, "anywhere", {}, "TCP", port))
        for port in (443, 6432, 9002, 10299, 10301):
            self.assertFalse(permits(self.policy, "source-dev", {"app": "source-pgbouncer"}, "TCP", port))
        self.assertFalse(permits(self.policy, "anywhere", {}, "UDP", 45001))

    def test_rejects_missing_duplicate_or_foreign_mapping_and_invalid_ports(self):
        cases = []
        data = observed_services()
        data["items"] = []
        cases.append(data)
        data = observed_services()
        data["items"].append(copy.deepcopy(data["items"][0]))
        cases.append(data)
        for key, value in (("namespace", "source-dev"), ("uid", "not-a-uid"), ("resourceVersion", "")):
            data = observed_services()
            data["items"][0]["metadata"][key] = value
            cases.append(data)
        for key, value in (("tailscale.com/proxy-group", "bravo-source-replication"),
                           ("tailscale.com/parent-resource", "target-db-for-replication"),
                           ("tailscale.com/parent-resource-ns", "target-dev"),
                           ("tailscale.com/parent-resource-type", "proxygroup"),
                           ("tailscale.com/managed", "false"), ("tailscale.com/svc-type", "ingress")):
            data = observed_services()
            data["items"][0]["metadata"]["labels"][key] = value
            cases.append(data)
        for key, value in (("name", "db"), ("port", 6432), ("protocol", "UDP"),
                           ("targetPort", 9999), ("targetPort", 11000), ("targetPort", "postgres")):
            data = observed_services()
            data["items"][0]["spec"]["ports"][0][key] = value
            cases.append(data)
        data = observed_services()
        data["items"][0]["spec"]["selector"] = {"app": "other"}
        cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                render.proxy_ingress(data, transport_ports=[45000])

    def test_accepts_unrelated_service_without_granting_its_ports(self):
        data = observed_services()
        unrelated = copy.deepcopy(data["items"][0])
        unrelated["metadata"]["labels"]["tailscale.com/proxy-group"] = "bravo-source-replication"
        unrelated["spec"]["ports"][0]["targetPort"] = 10400
        data["items"].append(unrelated)
        self.assertEqual(render.proxy_ingress(data, transport_ports=[45000, 46000]), self.policy)

    def test_requires_one_or_two_unique_observed_transport_ports(self):
        with self.assertRaises(TypeError):
            render.proxy_ingress(observed_services())
        for ports in (None, [], 45000, [True], [0], [65536], ["45000"], [45000, 45000],
                      [45000, 46000, 47000], [443], [5432], [6432], [9002], [10300], [45000, 10300]):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                render.proxy_ingress(observed_services(), transport_ports=ports)
        single = render.proxy_ingress(observed_services(), transport_ports=[45000])
        self.assertEqual(single["spec"]["ingress"][-1]["ports"], [{"protocol": "UDP", "port": 45000}])

    def test_ingress_cli_requires_current_observations_and_preserves_route_cli(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            render.main(["proxy-ingress"])
        self.assertEqual(error.exception.code, 2)
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(observed_services()))), redirect_stdout(output):
            render.main(["proxy-ingress", "--transport-port", "46000", "--transport-port", "45000"])
        self.assertEqual(json.loads(output.getvalue()), self.policy)


if __name__ == "__main__":
    unittest.main()
