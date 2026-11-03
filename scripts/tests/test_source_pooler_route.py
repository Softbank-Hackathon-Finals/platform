"""Reject wrong or stale routes before preparing the source pooler patch."""

import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("source_pooler_route", ROOT / "infra/envs/source-workloads/pooler_route.py")
route_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route_module)


def resources():
    deployment = {"kind": "Deployment", "metadata": {"name": "source-pgbouncer", "namespace": "source-dev", "resourceVersion": "41"},
                  "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "template": {"spec": {
                      "volumes": [{"name": "ca", "secret": {"secretName": "cutover-backend-ca"}}],
                      "hostAliases": [{"ip": "10.96.0.2", "hostnames": ["unrelated.example"]}]}}}}
    parent = {"kind": "Service", "metadata": {"name": "target-db-for-pooler", "namespace": "source-dev",
              "annotations": {"tailscale.com/proxy-group": "bravo-source-pooler"}},
              "spec": {"type": "ExternalName", "externalName": "ts-target-db-for-pooler-test.tailscale-system.svc.cluster.local"},
              "status": {"conditions": [{"type": "TailscaleEgressSvcReady", "status": "True"}]}}
    route = {"kind": "Service", "metadata": {"name": "ts-target-db-for-pooler-test", "namespace": "tailscale-system", "labels": {
        "tailscale.com/managed": "true", "tailscale.com/proxy-group": "bravo-source-pooler",
        "tailscale.com/parent-resource": "target-db-for-pooler", "tailscale.com/parent-resource-ns": "source-dev"}},
        "spec": {"type": "ClusterIP", "clusterIP": "10.96.0.42", "ports": [{"port": 5432, "targetPort": 10060, "protocol": "TCP"}]}}
    return deployment, parent, route


class CutoverPoolerRouteTests(unittest.TestCase):
    def test_only_alias_added_and_repeat_is_noop(self):
        deployment, parent, route = resources()
        before = deepcopy((deployment, parent, route))
        patches = route_module.target_route_patch(deployment, parent, route)
        self.assertEqual((deployment, parent, route), before)
        self.assertEqual(patches[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "41"})
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[1]["path"], "/spec/template/spec/hostAliases")
        self.assertEqual(patches[1]["value"][0], deployment["spec"]["template"]["spec"]["hostAliases"][0])
        deployment["spec"]["template"]["spec"]["hostAliases"] = patches[1]["value"]
        self.assertEqual(route_module.target_route_patch(deployment, parent, route), patches[:1])

    def test_conflicting_or_duplicate_hostname_mapping_is_rejected(self):
        for aliases in ([{"ip": "10.96.0.99", "hostnames": [route_module.TARGET_HOST]}],
                        [{"ip": "10.96.0.42", "hostnames": [route_module.TARGET_HOST]}] * 2):
            deployment, parent, route = resources()
            deployment["spec"]["template"]["spec"]["hostAliases"] = aliases
            with self.assertRaises(ValueError):
                route_module.target_route_patch(deployment, parent, route)

    def test_unready_wrong_route_and_unsafe_pooler_fail_closed(self):
        mutations = [
            lambda d, p, r: d["spec"].update(replicas=2),
            lambda d, p, r: d["metadata"].update(namespace="target-dev"),
            lambda d, p, r: d["spec"]["template"]["spec"]["volumes"][0]["secret"].update(secretName="source-only-ca"),
            lambda d, p, r: p["status"].update(conditions=[]),
            lambda d, p, r: p["spec"].update(externalName="unrelated.example"),
            lambda d, p, r: r["metadata"]["labels"].update({"tailscale.com/proxy-group": "replication"}),
            lambda d, p, r: r["spec"]["ports"][0].update(targetPort=9002),
            lambda d, p, r: r["spec"].update(clusterIP="127.0.0.1"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                values = resources()
                mutation(*values)
                with self.assertRaises(ValueError):
                    route_module.target_route_patch(*values)


if __name__ == "__main__":
    unittest.main()
