"""Independent request matrices and bad readbacks for the API access boundary."""

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "rehearsal_access", ROOT / "infra/envs/rehearsal-access/access.py"
)
access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(access)


def observed():
    return {
        "items": [
            {
                "kind": "Service",
                "metadata": {
                    "name": "generated-" + role,
                    "namespace": "tailscale-system",
                    "resourceVersion": "123",
                    "uid": f"00000000-0000-4000-8000-{i:012d}",
                    "labels": {
                        "tailscale.com/managed": "true",
                        "tailscale.com/parent-resource": "target-api",
                        "tailscale.com/parent-resource-ns": "bravo-"
                        + role
                        + "-control",
                        "tailscale.com/parent-resource-type": "svc",
                        "tailscale.com/proxy-group": "bravo-" + role + "-egress",
                        "tailscale.com/svc-type": "egress",
                    },
                },
                "spec": {
                    "ports": [
                        {
                            "name": "https",
                            "port": 443,
                            "protocol": "TCP",
                            "targetPort": port,
                        },
                        {
                            "name": "tailscale-health-check",
                            "port": 9002,
                            "protocol": "TCP",
                            "targetPort": 9002,
                        },
                    ]
                },
            }
            for i, (role, port) in enumerate(
                [("rehearsal", 10193), ("apply", 10384)], 1
            )
        ]
    }


def allowed(role, verb, resource, api_group, resource_name):
    return any(
        verb in rule["verbs"]
        and resource in rule["resources"]
        and api_group in rule["apiGroups"]
        and ("resourceNames" not in rule or resource_name in rule["resourceNames"])
        for rule in role["rules"]
    )


def permits(policy, namespace, labels, port):
    for rule in policy["spec"].get("ingress", []):
        if not any(
            p == {"protocol": "TCP", "port": port} for p in rule.get("ports", [])
        ):
            continue
        for source in rule["from"]:
            if source["namespaceSelector"]["matchLabels"] != {
                "kubernetes.io/metadata.name": namespace
            }:
                continue
            if all(
                labels.get(k) == v
                for k, v in source["podSelector"]["matchLabels"].items()
            ):
                return True
    return False


class AccessTests(unittest.TestCase):
    def test_collector_route_does_not_change_existing_identities_or_shared_resources(
        self,
    ):
        items = access.collector_platform("target-api.example.ts.net")
        self.assertEqual(
            [o["kind"] for o in items],
            [
                "NetworkPolicy",
                "NetworkPolicy",
                "ProxyGroup",
                "ProxyGroupPolicy",
                "Service",
            ],
        )
        self.assertEqual(
            items[0]["spec"]["podSelector"]["matchLabels"],
            {
                "app.kubernetes.io/name": "t10-registration",
                "bravo.dev/target-api-client": "collector",
            },
        )
        self.assertEqual(items[0]["metadata"]["namespace"], "platform-state")
        self.assertEqual(
            items[0]["spec"]["egress"][0]["ports"],
            [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        )
        self.assertEqual(len(items[0]["spec"]["egress"]), 1)
        self.assertEqual(items[2]["spec"]["tags"], ["tag:bravo-collector"])
        self.assertEqual(items[3]["spec"], {"egress": ["bravo-collector-egress"]})
        self.assertEqual(items[4]["metadata"]["name"], "collector-target-api")

    def test_collector_only_named_caller_and_exact_observed_port_can_enter_proxy(self):
        sample = observed()["items"][0]
        sample["metadata"]["labels"].update(
            {
                "tailscale.com/parent-resource": "collector-target-api",
                "tailscale.com/parent-resource-ns": "platform-state",
                "tailscale.com/proxy-group": "bravo-collector-egress",
            }
        )
        policies = access.collector_network({"items": [sample]})
        labels = {
            "app.kubernetes.io/name": "t10-registration",
            "bravo.dev/target-api-client": "collector",
        }
        ingress = policies[1]
        self.assertTrue(permits(ingress, "platform-state", labels, 10193))
        for ns, pod, port in [
            ("bravo-apply-control", labels, 10193),
            ("bravo-rehearsal-control", labels, 10193),
            ("platform-state", {"app.kubernetes.io/name": "t10-registration"}, 10193),
            ("platform-state", {"bravo.dev/target-api-client": "collector"}, 10193),
            ("platform-state", labels, 10194),
            ("platform-state", labels, 9002),
            ("platform-state", labels, 5432),
        ]:
            self.assertFalse(permits(ingress, ns, pod, port))
        for mutate in [
            lambda o: o["metadata"].update(uid=""),
            lambda o: o["metadata"].update(resourceVersion=""),
            lambda o: o["metadata"]["labels"].update(
                {"tailscale.com/parent-resource": "target-api"}
            ),
            lambda o: o["spec"]["ports"][0].update(targetPort=9002),
        ]:
            invalid = copy.deepcopy(sample)
            mutate(invalid)
            with self.assertRaises(ValueError):
                access.collector_network({"items": [invalid]})
        for bad in [{"items": []}, {"items": [sample, sample]}]:
            with self.assertRaises(ValueError):
                access.collector_network(bad)
        config = access.kubeconfig(
            "collector", "target-api.example.ts.net", "/credentials/token"
        )
        self.assertEqual(
            config["clusters"][0]["cluster"],
            {
                "server": "https://collector-target-api.platform-state.svc.cluster.local",
                "tls-server-name": "target-api.example.ts.net",
            },
        )
        self.assertEqual(
            config["users"][0]["user"], {"tokenFile": "/credentials/token"}
        )

    def test_bootstrap_is_closed_until_managed_service_ports_are_read(self):
        items = access.platform("target-api.example.ts.net", "scoped-operator-oauth")
        self.assertNotIn("Secret", [x["kind"] for x in items])
        proxy = next(x for x in items if x["kind"] == "ProxyClass")
        self.assertIn(
            "tailscale/tailscale:",
            proxy["spec"]["statefulSet"]["pod"]["tailscaleContainer"]["image"],
        )
        for obj in items:
            if (
                obj["kind"] == "NetworkPolicy"
                and obj["metadata"]["name"] == "target-api-client"
            ):
                self.assertEqual(
                    {p["port"] for r in obj["spec"]["egress"] for p in r["ports"]}, {53}
                )
            if obj["kind"] == "ServiceAccount":
                self.assertFalse(obj["automountServiceAccountToken"])

    def test_cross_role_and_unlabelled_access_cannot_enter_proxy(self):
        policies = access.network(observed())
        for role, port in [("rehearsal", 10193), ("apply", 10384)]:
            policy = next(
                x for x in policies if x["metadata"]["name"] == role + "-proxy-ingress"
            )
            self.assertTrue(
                permits(
                    policy,
                    "bravo-" + role + "-control",
                    {"bravo.dev/target-api-client": role},
                    port,
                )
            )
            other = "apply" if role == "rehearsal" else "rehearsal"
            self.assertFalse(
                permits(
                    policy,
                    "bravo-" + other + "-control",
                    {"bravo.dev/target-api-client": role},
                    port,
                )
            )
            self.assertFalse(permits(policy, "bravo-" + role + "-control", {}, port))
            self.assertFalse(
                permits(
                    policy,
                    "bravo-" + role + "-control",
                    {"bravo.dev/target-api-client": role},
                    9002,
                )
            )
            self.assertTrue(
                permits(policy, "tailscale-system", {"app": "operator"}, 9002)
            )

    def test_readback_rejects_missing_duplicate_foreign_or_unexpected_port(self):
        cases = []
        x = observed()
        x["items"].pop()
        cases.append(x)
        x = observed()
        x["items"].append(copy.deepcopy(x["items"][0]))
        cases.append(x)
        for field, value in [
            ("uid", ""),
            ("uid", True),
            ("namespace", "foreign"),
            ("resourceVersion", ""),
        ]:
            x = observed()
            x["items"][0]["metadata"][field] = value
            cases.append(x)
        for value in [9002, 9999, 11000, True, "10193"]:
            x = observed()
            x["items"][0]["spec"]["ports"][0]["targetPort"] = value
            cases.append(x)
        x = observed()
        x["items"][0]["metadata"]["labels"]["tailscale.com/proxy-group"] = "other"
        cases.append(x)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                access.network(case)

    def test_a_pull_secret_adds_only_that_one_secret_and_its_empty_placeholder(self):
        items = access.target_apply("target-fixture", "demo-chat", "bravo-ghcr-reader")
        role = next(x for x in items if x["kind"] == "Role")
        for verb in ["get", "update"]:
            self.assertTrue(allowed(role, verb, "secrets", "", "bravo-ghcr-reader"))
            self.assertFalse(allowed(role, verb, "secrets", "", "target-app-credentials"))
        self.assertFalse(allowed(role, "create", "secrets", "", "bravo-ghcr-reader"))
        [secret] = [x for x in items if x["kind"] == "Secret"]
        self.assertEqual(secret["metadata"]["namespace"], "target-fixture")
        self.assertEqual(secret["stringData"], {".dockerconfigjson": '{"auths":{}}'})
        self.assertFalse([x for x in access.target_apply("target-fixture", "demo-chat") if x["kind"] == "Secret"])

    def test_apply_identity_has_only_named_deployment_and_pod_observation(self):
        items = access.target_apply("target-fixture", "demo-chat")
        role = next(x for x in items if x["kind"] == "Role")
        for verb in ["get", "watch", "patch"]:
            self.assertTrue(allowed(role, verb, "deployments", "apps", "demo-chat"))
            self.assertFalse(allowed(role, verb, "deployments", "apps", "database"))
        for verb, resource, group in [
            ("delete", "deployments", "apps"),
            ("create", "deployments", "apps"),
            ("get", "secrets", ""),
            ("create", "pods/exec", ""),
            ("create", "rolebindings", "rbac.authorization.k8s.io"),
        ]:
            self.assertFalse(allowed(role, verb, resource, group, "demo-chat"))
        self.assertTrue(allowed(role, "list", "pods", "", ""))
        self.assertFalse(any(x["kind"] == "ClusterRole" for x in items))
        for ns in ["kube-system", "default", "tailscale-system", "bravo-apply-access"]:
            with self.assertRaises(ValueError):
                access.target_apply(ns, "demo-chat")

    def test_kubeconfig_preserves_service_dns_and_tls_identity_without_secret(self):
        obj = access.kubeconfig(
            "apply", "target-api.example.ts.net", "/credentials/token"
        )
        cluster = obj["clusters"][0]["cluster"]
        self.assertEqual(cluster["tls-server-name"], "target-api.example.ts.net")
        self.assertEqual(
            cluster["server"],
            "https://target-api.bravo-apply-control.svc.cluster.local",
        )
        self.assertNotIn("insecure-skip-tls-verify", cluster)
        self.assertEqual(obj["users"][0]["user"], {"tokenFile": "/credentials/token"})
        for value in [
            "https://target-api.example.ts.net",
            "target-api.example.ts.net.evil.invalid",
            "100.64.0.1",
            "target-api.example.ts.net.",
        ]:
            with self.assertRaises(ValueError):
                access.kubeconfig("apply", value, "/credentials/token")
        for path in ["token", "/../token", "/"]:
            with self.assertRaises(ValueError):
                access.kubeconfig("apply", "target-api.example.ts.net", path)


if __name__ == "__main__":
    unittest.main()
