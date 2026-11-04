#!/usr/bin/env python3
"""Render each Collector's dedicated tailnet route to the platform metrics store; never register nodes or change ACLs."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "executor-network"))
from control_network import (
    OPERATOR_NAMESPACE,
    PROXY_LABELS,
    observed_proxy_ports,
    proxy_ingress_policy,
)

NAMESPACE = "bravo-observability"
SERVICE = "bravo-metrics"
PORT = 9090
CALLER = {"app": "bravo-collector"}
SIDES = ("source", "target")


def group(side):
    if side not in SIDES:
        raise ValueError("source or target required")
    return f"bravo-{side}-collector"


def _labels(policy, side):
    policy["metadata"]["labels"] = {"component": side, "app.kubernetes.io/part-of": "bravo-collector-network"}
    return policy


def routes(side, metrics_fqdn):
    """One ProxyGroup per side so the tailnet sees the Collector, not the node or another workload."""
    name = group(side)
    if not isinstance(metrics_fqdn, str) or not re.fullmatch(
        r"bravo-metrics\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net", metrics_fqdn
    ):
        raise ValueError("verified metrics store's exact MagicDNS FQDN required")
    return [
        {"apiVersion": "tailscale.com/v1alpha1", "kind": "ProxyGroup",
         "metadata": {"name": name, "labels": {"component": side}},
         "spec": {"type": "egress", "replicas": 1, "tags": ["tag:" + name]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": SERVICE, "namespace": NAMESPACE,
            "labels": {"component": side, "app.kubernetes.io/part-of": "bravo-collector-network"},
            "annotations": {"tailscale.com/tailnet-fqdn": metrics_fqdn, "tailscale.com/proxy-group": name},
        }, "spec": {"type": "ExternalName", "externalName": "placeholder",
                    "ports": [{"name": "otlp", "port": PORT, "protocol": "TCP"}]}},
    ]


def _ports(side, observed_services):
    return observed_proxy_ports(observed_services, group=group(side), namespace=NAMESPACE,
                                expected={SERVICE: ("otlp", PORT)})


def collector_egress(side, observed_services):
    """Collector → only this side's proxy Pods, on the service port and the observed post-DNAT port."""
    ports = _ports(side, observed_services)
    return _labels({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": {"name": "bravo-collector-tailnet", "namespace": NAMESPACE},
                    "spec": {"podSelector": {"matchLabels": CALLER.copy()}, "policyTypes": ["Egress"],
                             "egress": [{"to": [{
                                 "namespaceSelector": {"matchLabels": {
                                     "kubernetes.io/metadata.name": OPERATOR_NAMESPACE}},
                                 "podSelector": {"matchLabels": {
                                     **PROXY_LABELS, "tailscale.com/parent-resource": group(side)}},
                             }], "ports": [{"protocol": "TCP", "port": port} for port in ports]}]}}, side)


def proxy_ingress(side, observed_services, *, transport_ports):
    policy = proxy_ingress_policy(_ports(side, observed_services), group=group(side), namespace=NAMESPACE,
                                  caller=CALLER, transport_ports=transport_ports,
                                  policy_name=f"{group(side)}-ingress")
    return _labels(policy, side)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["routes", "collector-egress", "proxy-ingress"])
    parser.add_argument("--side", required=True, choices=SIDES)
    parser.add_argument("--metrics-fqdn", help="independently verified bravo-metrics.<tailnet>.ts.net")
    parser.add_argument("--transport-port", type=int, action="append",
                        help="current proxy UDP listen port; repeat for a second address family")
    args = parser.parse_args(argv)
    if args.stage == "routes":
        if args.metrics_fqdn is None:
            parser.error("routes requires --metrics-fqdn")
        result = {"apiVersion": "v1", "kind": "List", "items": routes(args.side, args.metrics_fqdn)}
    elif args.stage == "collector-egress":
        result = collector_egress(args.side, json.load(sys.stdin))
    else:
        if args.transport_port is None:
            parser.error("proxy-ingress requires --transport-port from current Pod-bound readback")
        result = proxy_ingress(args.side, json.load(sys.stdin), transport_ports=args.transport_port)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
