"""Collector tailnet route: dedicated identity per side, exact observed ports and the ACL fragment."""
import copy
import importlib.util
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("collector_route", ROOT / "infra/envs/collector-network/collector_route.py")
render = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", list(sys.path)):
    SPEC.loader.exec_module(render)
FRAGMENT = json.loads((ROOT / "infra/envs/collector-network/tailnet-fragment.json").read_text())
FQDN = "bravo-metrics.example-tailnet.ts.net"
EMPTY_ROUTE = {"group": None, "service": None, "children": {"kind": "ServiceList", "items": []}}


def observed(side="source", target_port=10421, uid="00000000-0000-4000-8000-0000000000aa"):
    return {"apiVersion": "v1", "kind": "List", "items": [{
        "apiVersion": "v1", "kind": "Service", "metadata": {
            "name": "ts-bravo-metrics-fixture", "namespace": "tailscale-system", "resourceVersion": "7",
            "uid": uid, "labels": {
                "tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "svc",
                "tailscale.com/parent-resource": "bravo-metrics",
                "tailscale.com/parent-resource-ns": "bravo-observability",
                "tailscale.com/proxy-group": f"bravo-{side}-collector", "tailscale.com/svc-type": "egress",
            }}, "spec": {"type": "ClusterIP", "ports": [
                {"name": "otlp", "port": 9090, "targetPort": target_port, "protocol": "TCP"},
                {"name": "tailscale-health-check", "port": 9002, "targetPort": 9002, "protocol": "TCP"},
            ]}}]}


def quarantine_readback(side="source"):
    policy = render.proxy_quarantine(side)
    policy["metadata"].update(uid="fixture-uid", resourceVersion="1")
    return {"kind": "NetworkPolicyList", "items": [policy]}


class Routes(unittest.TestCase):
    def test_each_side_gets_its_own_proxy_identity(self):
        for side in ("source", "target"):
            group, service = render.routes(side, FQDN, quarantine_readback(side), existing=EMPTY_ROUTE)
            self.assertEqual(group["spec"], {"type": "egress", "replicas": 1, "tags": [f"tag:bravo-{side}-collector"]})
            self.assertEqual(service["metadata"]["namespace"], "bravo-observability")
            self.assertEqual(service["metadata"]["annotations"], {
                "tailscale.com/tailnet-fqdn": FQDN, "tailscale.com/proxy-group": f"bravo-{side}-collector"})
            self.assertEqual(service["spec"]["ports"], [{"name": "otlp", "port": 9090, "protocol": "TCP"}])

    def test_rejects_unverified_destinations(self):
        for bad in ("bravo-metrics.ts.net", "bravo-window-intake.example-tailnet.ts.net", FQDN + ".",
                    "bravo-metrics.example-tailnet.ts.net.evil", "BRAVO-metrics.example-tailnet.ts.net", None):
            with self.assertRaises(ValueError):
                render.routes("source", bad, quarantine_readback(), existing=EMPTY_ROUTE)
        with self.assertRaises(ValueError):
            render.routes("platform", FQDN, quarantine_readback(), existing=EMPTY_ROUTE)


class Policies(unittest.TestCase):
    def test_collector_reaches_only_its_proxy_on_observed_ports(self):
        policy = render.collector_egress("source", observed())
        spec = policy["spec"]
        self.assertEqual(policy["metadata"]["namespace"], "bravo-observability")
        self.assertEqual(spec["podSelector"], {"matchLabels": {"app": "bravo-collector"}})
        self.assertEqual(spec["policyTypes"], ["Egress"])
        (rule,) = spec["egress"]
        (peer,) = rule["to"]
        self.assertEqual(peer["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": "tailscale-system"}})
        self.assertEqual(peer["podSelector"]["matchLabels"]["tailscale.com/parent-resource"], "bravo-source-collector")
        self.assertEqual(peer["podSelector"]["matchLabels"]["tailscale.com/parent-resource-type"], "proxygroup")
        self.assertEqual(sorted(p["port"] for p in rule["ports"]), [9090, 10421])

    def test_pre_dnat_exception_is_exact_child_ip_and_only_business_port(self):
        for ip, prefix in (("172.20.1.2", 32), ("fd00::10", 128)):
            services = observed()
            services["items"][0]["spec"]["clusterIP"] = ip
            result = render.collector_egress("source", services, service_ip_egress=True)
            self.assertEqual(result["spec"]["egress"][1], {
                "to": [{"ipBlock": {"cidr": f"{ip}/{prefix}"}}],
                "ports": [{"protocol": "TCP", "port": 9090}],
            })
            self.assertEqual(len(result["spec"]["egress"]), 2)
        with self.assertRaises(ValueError):
            render.collector_egress("target", services, service_ip_egress=True)

    def test_pre_dnat_exception_rejects_missing_or_non_host_addresses(self):
        for ip in (None, "None", "", "172.20.0.0/16", "0.0.0.0", "127.0.0.1", "224.0.0.1", "::"):
            services = observed()
            services["items"][0]["spec"]["clusterIP"] = ip
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                render.collector_egress("source", services, service_ip_egress=True)
        with self.assertRaises(ValueError):
            render.collector_egress("source", observed(), service_ip_egress="true")

    def test_other_side_or_changed_mapping_is_refused(self):
        with self.assertRaises(ValueError):
            render.collector_egress("target", observed("source"))
        for change in (
            lambda s: s["metadata"]["labels"].update({"tailscale.com/parent-resource-ns": "app"}),
            lambda s: s["spec"]["ports"][0].update({"targetPort": 12000}),
            lambda s: s["spec"]["ports"][0].update({"port": 9091}),
            lambda s: s["spec"].update({"selector": {"app": "x"}}),
            lambda s: s["metadata"].update({"uid": "not-a-uid"}),
        ):
            services = observed()
            change(services["items"][0])
            with self.assertRaises(ValueError):
                render.collector_egress("source", services)
        services = observed()
        services["items"].append(copy.deepcopy(services["items"][0]))
        with self.assertRaises(ValueError):
            render.collector_egress("source", services)

    def test_proxy_accepts_only_the_collector(self):
        policy = render.proxy_ingress("target", observed("target"), transport_ports=[41641])
        self.assertEqual(policy["metadata"]["namespace"], "tailscale-system")
        self.assertEqual(policy["metadata"]["labels"]["component"], "target")
        business, health, transport = policy["spec"]["ingress"]
        self.assertEqual(business["from"][0]["podSelector"], {"matchLabels": {"app": "bravo-collector"}})
        self.assertEqual(business["from"][0]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "bravo-observability"})
        self.assertEqual(sorted(p["port"] for p in business["ports"]), [9090, 10421])
        self.assertEqual(health["ports"], [{"protocol": "TCP", "port": 9002}])
        self.assertEqual(transport["ports"], [{"protocol": "UDP", "port": 41641}])
        with self.assertRaises(ValueError):
            render.proxy_ingress("target", observed("target"), transport_ports=[9090])

    def test_cli_refuses_routes_now_owned_by_iac(self):
        # The tailnet-egress IaC module owns these objects (T44); the CLI no longer renders a second copy.
        for stage in ("proxy-quarantine", "routes", "collector-egress", "proxy-ingress"):
            err = io.StringIO()
            with self.subTest(stage=stage), patch("sys.stderr", err), redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                render.main([stage, "--side", "source"])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("infra/iac/environments", err.getvalue())


class Quarantine(unittest.TestCase):
    def test_no_routes_before_server_readback(self):
        for readback in ({}, {"kind": "NetworkPolicyList", "items": []},
                         {"kind": "NetworkPolicyList", "items": [render.proxy_quarantine("source")]}):
            with self.assertRaises(ValueError):
                render.routes("source", FQDN, readback, existing=EMPTY_ROUTE)

    def test_bootstrap_selects_same_proxy_as_final_policy(self):
        bootstrap = render.proxy_quarantine("target")
        final = render.proxy_ingress("target", observed("target"), transport_ports=[41641])
        self.assertEqual(bootstrap["metadata"], final["metadata"])
        self.assertEqual(bootstrap["spec"]["podSelector"], final["spec"]["podSelector"])
        self.assertEqual(bootstrap["spec"]["ingress"], [])

    def test_additive_allow_partial_or_wrong_side_is_rejected(self):
        for mutation in (
            lambda d: d["items"].append({"kind": "NetworkPolicy", "metadata": {
                "namespace": "tailscale-system", "name": "open"},
                "spec": {"podSelector": {}, "ingress": [{}]}}),
            lambda d: d.update(metadata={"continue": "more"}),
            lambda d: d["items"][0]["metadata"].update(deletionTimestamp="now"),
            lambda d: d["items"][0]["spec"].update(ingress=[{}]),
            lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
        ):
            data = quarantine_readback()
            mutation(data)
            with self.assertRaises(ValueError):
                render.routes("source", FQDN, data, existing=EMPTY_ROUTE)
        with self.assertRaises(ValueError):
            render.routes("target", FQDN, quarantine_readback("source"), existing=EMPTY_ROUTE)

    def test_apiserver_may_omit_empty_ingress(self):
        data = quarantine_readback()
        del data["items"][0]["spec"]["ingress"]
        self.assertEqual(len(render.routes("source", FQDN, data, existing=EMPTY_ROUTE)), 2)

    def test_other_groups_policy_does_not_block_route(self):
        data = quarantine_readback()
        other = render.proxy_ingress("target", observed("target"), transport_ports=[41641])
        data["items"].append(other)
        self.assertEqual(len(render.routes("source", FQDN, data, existing=EMPTY_ROUTE)), 2)


class Resume(unittest.TestCase):
    def state(self):
        group, service = render.routes("source", FQDN, quarantine_readback(), existing=EMPTY_ROUTE)
        for obj in (group, service):
            obj["metadata"].update(uid="fixture-uid", resourceVersion="7")
        service["spec"]["externalName"] = "ts-bravo-metrics-fixture.tailscale-system.svc.cluster.local"
        return {"group": group, "service": service, "children": observed()}

    def test_operator_update_and_lost_response_resume_are_noop(self):
        state = self.state()
        before = copy.deepcopy(state)
        for _ in range(2):
            self.assertEqual(render.routes("source", FQDN, quarantine_readback(), existing=state), [])
        self.assertEqual(state, before)

    def test_only_validated_group_child_can_supply_external_name(self):
        state = self.state()
        other = copy.deepcopy(state["children"]["items"][0])
        other["metadata"]["name"] = "ts-other-group"
        other["metadata"]["labels"]["tailscale.com/proxy-group"] = "other-group"
        state["children"]["items"].append(other)
        self.assertEqual(render.routes("source", FQDN, quarantine_readback(), existing=state), [])
        state["service"]["spec"]["externalName"] = "ts-other-group.tailscale-system.svc.cluster.local"
        with self.assertRaisesRegex(ValueError, "does not reference the observed child"):
            render.routes("source", FQDN, quarantine_readback(), existing=state)

    def test_partial_create_only_outputs_missing_service(self):
        state = self.state()
        state.update(service=None, children={"kind": "ServiceList", "items": []})
        pending = render.routes("source", FQDN, quarantine_readback(), existing=state)
        self.assertEqual([obj["kind"] for obj in pending], ["Service"])
        self.assertEqual(pending[0]["spec"]["externalName"], "placeholder")

    def test_pending_operator_update_keeps_original_service_untouched(self):
        state = self.state()
        state["service"]["spec"]["externalName"] = "placeholder"
        state["children"]["items"] = []
        self.assertEqual(render.routes("source", FQDN, quarantine_readback(), existing=state), [])

    def test_wrong_group_child_or_route_cannot_be_adopted(self):
        for mutation in (
            lambda d: d["service"]["spec"].update(externalName="evil.example"),
            lambda d: d["service"]["metadata"]["annotations"].update({"tailscale.com/proxy-group": "other"}),
            lambda d: d["group"]["spec"].update(tags=["tag:bravo-cutover-control"]),
            lambda d: d.update(group=None),
            lambda d: d["children"].update(metadata={"continue": "partial"}),
            lambda d: d["children"]["items"][0]["metadata"]["labels"].update({"tailscale.com/proxy-group": "other"}),
        ):
            state = self.state()
            mutation(state)
            with self.assertRaises(ValueError):
                render.routes("source", FQDN, quarantine_readback(), existing=state)


class Fragment(unittest.TestCase):
    def test_only_collectors_reach_the_store_and_only_on_9090(self):
        self.assertEqual(FRAGMENT["grants"], [{"src": ["tag:bravo-source-collector", "tag:bravo-target-collector"],
                                               "dst": ["tag:bravo-metrics"], "ip": ["tcp:9090"]}])
        self.assertEqual(FRAGMENT["tagOwners"]["tag:bravo-source-collector"], ["tag:bravo-source-operator"])
        self.assertEqual(FRAGMENT["tagOwners"]["tag:bravo-target-collector"], ["tag:bravo-target-operator"])
        self.assertEqual(FRAGMENT["tagOwners"]["tag:bravo-metrics"], ["tag:bravo-platform-operator"])

    def test_policy_tests_deny_every_other_known_tag(self):
        accepts = {t["src"] for t in FRAGMENT["tests"] if "tag:bravo-metrics:9090" in t.get("accept", [])}
        denies = {t["src"] for t in FRAGMENT["tests"] if "tag:bravo-metrics:9090" in t.get("deny", [])}
        self.assertEqual(accepts, {"tag:bravo-source-collector", "tag:bravo-target-collector"})
        # Every tag another tailnet fragment or renderer in this repository grants or assigns.
        known = set()
        for path in ROOT.glob("**/*"):
            if path.suffix in {".json", ".py"} and path.is_file() and ".git" not in path.parts:
                # A tag name never ends with "-"; invalid examples in tests are not tags.
                known |= set(re.findall(r"tag:bravo-[a-z0-9]+(?:-[a-z0-9]+)*", path.read_text(errors="ignore")))
        known -= {"tag:bravo-metrics", "tag:bravo-source-collector", "tag:bravo-target-collector"}
        self.assertTrue(known)
        self.assertLessEqual(known, denies)


class Collector(unittest.TestCase):
    def test_store_name_is_verified_through_the_egress_service(self):
        config = (ROOT / "services/observability/deploy/collector/collector.yaml").read_text()
        self.assertIn("server_name_override: ${env:PLATFORM_TLS_SERVER_NAME:-}", config)

    def test_no_9090_egress_to_any_destination(self):
        resources = (ROOT / "services/observability/deploy/collector/resources.yaml").read_text()
        policy = resources.split("kind: NetworkPolicy", 1)[1]
        egress = policy.split("  egress:", 1)[1]
        rules = re.split(r"\n    - ", egress)
        for rule in rules:
            if "9090" in rule:
                self.assertIn("to:", rule)
                self.assertIn("app: bravo-prometheus", rule)


if __name__ == "__main__":
    unittest.main()
