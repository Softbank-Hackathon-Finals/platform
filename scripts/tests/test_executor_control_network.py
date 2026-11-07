"""Bind Executor routes to exact hosts and observed operator port mappings."""
import copy
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("executor_control_network", ROOT / "infra/envs/executor-network/control_network.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


def fqdns(api=False):
    result = {"source_db": "bravo-source-db.example.ts.net", "target_db": "bravo-target-db.example.ts.net",
              "source_pooler": "bravo-source-control-pooler.example.ts.net",
              "target_pooler": "bravo-target-control-pooler.example.ts.net"}
    if api:
        result.update({"source_api": "bravo-source-operator.example.ts.net", "target_api": "bravo-target-operator.example.ts.net"})
    return result


def observed_services(api=False):
    items = []
    for index, (side, kind, _, port) in enumerate(render.route_contract(api)):
        parent = f"{side}-{kind}-for-cutover"
        items.append({"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": f"ts-fixture-{index}", "namespace": "tailscale-system", "resourceVersion": "100",
            "uid": f"00000000-0000-4000-8000-{index + 1:012d}", "labels": {
                "tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "svc",
                "tailscale.com/parent-resource": parent, "tailscale.com/parent-resource-ns": "bravo-platform",
                "tailscale.com/proxy-group": "bravo-cutover-control", "tailscale.com/svc-type": "egress",
            }}, "spec": {"type": "ClusterIP", "ports": [
                {"name": kind, "port": port, "targetPort": 10200 + index, "protocol": "TCP"},
                {"name": "tailscale-health-check", "port": 9002, "targetPort": 9002, "protocol": "TCP"},
            ]}})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def ingress_permits(policy, namespace, labels, protocol, port):
    """Evaluate this policy's matchLabels/port rules, including transport-only ingress."""
    for rule in policy["spec"]["ingress"]:
        if not any(p["protocol"] == protocol and p["port"] == port for p in rule["ports"]):
            continue
        if "from" not in rule:
            return True
        for peer in rule["from"]:
            expected_namespace = peer["namespaceSelector"]["matchLabels"]
            expected_labels = peer["podSelector"]["matchLabels"]
            if expected_namespace == {"kubernetes.io/metadata.name": namespace} and all(
                labels.get(key) == value for key, value in expected_labels.items()
            ):
                return True
    return False


class RouteBoundaryTests(unittest.TestCase):
    def test_platform_operator_uses_precreated_credentials_and_an_auth_mode_api_proxy(self):
        values = render.values()
        self.assertEqual(values["oauth"], {"clientId": "", "clientSecret": ""})
        self.assertEqual(values["oauthSecretVolume"], {"secret": {"secretName": "operator-oauth"}})
        # Auth mode only: the proxy impersonates the group a tailnet grant names; noauth would pass the
        # operator's own rights through to every tailnet caller.
        self.assertEqual(values["apiServerProxyConfig"], {"mode": "true", "allowImpersonation": "false"})
        self.assertEqual(values["operatorConfig"]["defaultTags"], ["tag:bravo-platform-operator"])
        self.assertEqual(values["proxyConfig"]["defaultTags"], "tag:bravo-cutover-control")
        for image in (values["operatorConfig"]["image"], values["proxyConfig"]["image"]):
            self.assertRegex(image["digest"], r"^sha256:[a-f0-9]{64}$")

    def test_default_routes_have_only_four_data_services_and_one_proxy_group(self):
        items = render.routes(fqdns())
        group, *services = items
        self.assertEqual(group["kind"], "ProxyGroup")
        self.assertNotIn("namespace", group["metadata"])
        self.assertEqual(group["spec"], {"type": "egress", "replicas": 1, "tags": ["tag:bravo-cutover-control"]})
        self.assertEqual({s["metadata"]["name"] for s in services}, {
            "source-db-for-cutover", "source-pooler-for-cutover", "target-db-for-cutover", "target-pooler-for-cutover"})
        self.assertEqual({p["port"] for s in services for p in s["spec"]["ports"]}, {5432, 6432})
        for service in services:
            self.assertEqual(service["metadata"]["namespace"], "bravo-platform")
            self.assertEqual(service["spec"]["type"], "ExternalName")
            self.assertNotIn("selector", service["spec"])
            self.assertNotIn("targetPort", service["spec"]["ports"][0])

    def test_api_routes_require_explicit_flag_and_both_verified_hosts(self):
        with self.assertRaises(ValueError):
            render.routes(fqdns(True))
        with self.assertRaises(ValueError):
            render.routes(fqdns(), True)
        items = render.routes(fqdns(True), True)
        api = [s for s in items if s["metadata"]["name"].endswith("api-for-cutover")]
        self.assertEqual(len(api), 2)
        self.assertTrue(all(s["spec"]["ports"] == [{"name": "api", "port": 443, "protocol": "TCP"}] for s in api))

    def test_rejects_wrong_side_service_tailnet_or_noncanonical_fqdn(self):
        for hostname in ["bravo-target-db.example.ts.net", "bravo-source-operator.example.ts.net",
                         "bravo-source-db.other.ts.net", "bravo-source-db.example.ts.net.evil.invalid",
                         "bravo-source-db.example.ts.net.", "https://bravo-source-db.example.ts.net",
                         "bravo-source-db.example.ts.net:5432", "100.100.100.100", None]:
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                render.routes(fqdns() | {"source_db": hostname})

    def test_pooler_route_adds_a_service_without_changing_app_port(self):
        for side in ("source", "target"):
            svc = render.pooler_ingress(side)
            self.assertEqual(svc["metadata"]["name"], f"{side}-control-pooler-tailnet")
            self.assertEqual(svc["metadata"]["namespace"], f"{side}-dev")
            self.assertEqual(svc["spec"]["selector"], {"app": f"{side}-pgbouncer"})
            self.assertEqual(svc["spec"]["ports"], [{"name": "pooler", "port": 6432, "targetPort": 5432, "protocol": "TCP"}])
            self.assertEqual(svc["metadata"]["annotations"]["tailscale.com/tags"], f"tag:bravo-{side}-control-pooler")
        with self.assertRaises(ValueError):
            render.pooler_ingress("platform")


class MappingBoundaryTests(unittest.TestCase):
    def test_network_includes_exact_translated_ports_and_excludes_health_port(self):
        policy = render.network(observed_services(), False)
        self.assertEqual(policy["metadata"]["namespace"], "bravo-platform")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "cutover-executor"}})
        rule = policy["spec"]["egress"][0]
        self.assertEqual(rule["to"], [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tailscale-system"}},
            "podSelector": {"matchLabels": {"tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "proxygroup",
                                             "tailscale.com/parent-resource": "bravo-cutover-control"}},
        }])
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": p} for p in [5432, 6432, 10200, 10201, 10202, 10203]])
        self.assertNotIn(9002, [p["port"] for p in rule["ports"]])
        self.assertNotIn(443, [p["port"] for p in rule["ports"]])
        self.assertNotIn(10500, [p["port"] for p in rule["ports"]])
        self.assertTrue(all("endPort" not in p for p in rule["ports"]))

    def test_allocator_boundaries_match_pinned_implementation(self):
        # Independent values from upstream unusedPort(): rand.Int32N(1000) + 10000.
        for target in (10000, 10999):
            data = observed_services()
            data["items"][0]["spec"]["ports"][0]["targetPort"] = target
            with self.subTest(target=target):
                self.assertEqual(render.mapped_ports(data), sorted([5432, 6432, target, 10201, 10202, 10203]))
        for target in (9999, 11000, 3200, 9002, True, "10200"):
            data = observed_services()
            data["items"][0]["spec"]["ports"][0]["targetPort"] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                render.mapped_ports(data)

    def test_fails_closed_on_missing_duplicate_or_foreign_service_readback(self):
        cases = []
        data = observed_services()
        data["items"].pop()
        cases.append(data)
        data = observed_services()
        data["items"].append(copy.deepcopy(data["items"][0]))
        cases.append(data)
        for field, value in [("namespace", "other-system"), ("uid", ""), ("resourceVersion", "")]:
            data = observed_services()
            data["items"][0]["metadata"][field] = value
            cases.append(data)
        for key, value in [("tailscale.com/proxy-group", "unrelated"), ("tailscale.com/parent-resource-ns", "source-dev"),
                           ("tailscale.com/parent-resource-type", "proxygroup"), ("tailscale.com/managed", "false")]:
            data = observed_services()
            data["items"][0]["metadata"]["labels"][key] = value
            cases.append(data)
        for field, value in [("protocol", "UDP"), ("port", 22), ("targetPort", 11000), ("targetPort", "postgres")]:
            data = observed_services()
            data["items"][0]["spec"]["ports"][0][field] = value
            cases.append(data)
        data = observed_services()
        data["items"][1]["spec"]["ports"][0]["targetPort"] = 10200
        cases.append(data)
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                render.network(case)

    def test_api_network_requires_explicit_opt_in_and_complete_six_route_readback(self):
        with self.assertRaises(ValueError):
            render.network(observed_services(True))
        with self.assertRaises(ValueError):
            render.network(observed_services(), True)
        ports = render.mapped_ports(observed_services(True), True)
        self.assertEqual(ports, [443, 5432, 6432, 10200, 10201, 10202, 10203, 10204, 10205])


class ProxyIngressBoundaryTests(unittest.TestCase):
    def setUp(self):
        # A synthetic observation, not an assumption about a runtime default.
        self.policy = render.proxy_ingress(observed_services(), transport_ports=[45000, 46000])

    def test_only_exact_executor_namespace_and_label_can_use_business_ports(self):
        caller = {"app.kubernetes.io/name": "cutover-executor"}
        for port in (5432, 6432, 10200, 10201, 10202, 10203):
            self.assertTrue(ingress_permits(self.policy, "bravo-platform", caller, "TCP", port))
            self.assertFalse(ingress_permits(self.policy, "other-platform", caller, "TCP", port))
            self.assertFalse(ingress_permits(self.policy, "bravo-platform", {"app.kubernetes.io/name": "collector"}, "TCP", port))
            self.assertFalse(ingress_permits(self.policy, "tailscale-system", {"app": "operator"}, "TCP", port))

    def test_operator_health_is_separate_from_executor_business_authority(self):
        self.assertTrue(ingress_permits(self.policy, "tailscale-system", {"app": "operator"}, "TCP", 9002))
        for namespace, labels in [("bravo-platform", {"app": "operator"}),
                                  ("tailscale-system", {"app": "unrelated"}),
                                  ("bravo-platform", {"app.kubernetes.io/name": "cutover-executor"})]:
            with self.subTest(namespace=namespace, labels=labels):
                self.assertFalse(ingress_permits(self.policy, namespace, labels, "TCP", 9002))

    def test_api_ingress_requires_opt_in_and_complete_observations(self):
        caller = {"app.kubernetes.io/name": "cutover-executor"}
        self.assertFalse(ingress_permits(self.policy, "bravo-platform", caller, "TCP", 443))
        with self.assertRaises(ValueError):
            render.proxy_ingress(observed_services(True), transport_ports=[45000])
        with self.assertRaises(ValueError):
            render.proxy_ingress(observed_services(), True, transport_ports=[45000])
        policy = render.proxy_ingress(observed_services(True), True, transport_ports=[45000])
        self.assertTrue(ingress_permits(policy, "bravo-platform", caller, "TCP", 443))
        self.assertFalse(ingress_permits(policy, "other-platform", caller, "TCP", 443))

    def test_transport_rule_does_not_open_tcp_business_or_other_proxy_groups(self):
        for port in (45000, 46000):
            self.assertTrue(ingress_permits(self.policy, "arbitrary", {}, "UDP", port))
            self.assertFalse(ingress_permits(self.policy, "arbitrary", {}, "TCP", port))
        for port in (5432, 6432, 9002, 10200, 44999, 45001, 45999, 46001):
            self.assertFalse(ingress_permits(self.policy, "arbitrary", {}, "UDP", port))
        self.assertEqual(self.policy["metadata"]["namespace"], "tailscale-system")
        self.assertEqual(self.policy["spec"]["podSelector"], {"matchLabels": {
            "tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "proxygroup",
            "tailscale.com/parent-resource": "bravo-cutover-control",
        }})
        self.assertEqual(self.policy["spec"]["policyTypes"], ["Ingress"])
        self.assertNotIn("egress", self.policy["spec"])

    def test_requires_observed_transport_ports_and_rejects_reserved_collisions(self):
        with self.assertRaises(TypeError):
            render.proxy_ingress(observed_services())
        for ports in (None, 45000, (45000,), [], [45000, 45000], [45000, 46000, 47000],
                      [True], [0], [-1], [65536], ["45000"], [443], [5432], [6432], [9002], [10200],
                      [45000, 9002], [45000, 10201]):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                render.proxy_ingress(observed_services(), transport_ports=ports)

    def test_transport_accepts_one_or_two_distinct_ports_with_deterministic_output(self):
        for ports in ([1], [65535], [45000], [46000, 45000]):
            with self.subTest(ports=ports):
                policy = render.proxy_ingress(observed_services(), transport_ports=ports)
                self.assertEqual(policy["spec"]["ingress"][-1]["ports"],
                                 [{"protocol": "UDP", "port": port} for port in sorted(ports)])

    def test_route_stages_are_refused_because_the_iac_module_owns_them(self):
        for stage in ("routes", "network", "proxy-ingress", "pooler-ingress"):
            err = io.StringIO()
            with self.subTest(stage=stage), patch.object(render.sys, "stderr", err), \
                    self.assertRaises(SystemExit) as error:
                render.main([stage, "--transport-port", "46000", "--side", "source"])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("infra/iac/environments", err.getvalue())
        policy = render.proxy_ingress(observed_services(), False, transport_ports=[46000, 45000])
        self.assertEqual(policy, self.policy)


if __name__ == "__main__":
    unittest.main()
