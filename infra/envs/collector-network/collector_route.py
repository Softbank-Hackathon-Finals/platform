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


def proxy_quarantine(side):
    """Install before creating any proxy; replace this same policy after port readback."""
    return _labels({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": {"name": f"{group(side)}-ingress", "namespace": OPERATOR_NAMESPACE},
                    "spec": {"podSelector": {"matchLabels": {
                        **PROXY_LABELS, "tailscale.com/parent-resource": group(side)}},
                             "policyTypes": ["Ingress"], "ingress": []}}, side)


def _may_select(selector, labels):
    # Conservative for additional Pod labels and expressions: unknown means overlap.
    if not isinstance(selector, dict):
        return True
    matches = selector.get("matchLabels", {})
    if not isinstance(matches, dict):
        return True
    return not any(key in labels and labels[key] != value for key, value in matches.items())


def verify_quarantine(side, readback):
    """Require a server readback, including potentially additive ingress policies.

    This is a render precondition, not a live enforcement attestation. The operator
    must read back immediately before route creation and test the effective path.
    """
    expected = proxy_quarantine(side)
    if not isinstance(readback, dict) or readback.get("kind") not in ("List", "NetworkPolicyList"):
        raise ValueError("complete tailscale-system NetworkPolicy list readback required")
    if readback.get("metadata", {}).get("continue"):
        raise ValueError("paginated policy readback is incomplete")
    policies = readback.get("items")
    if not isinstance(policies, list):
        raise TypeError("policy items required")
    found = 0
    labels = expected["spec"]["podSelector"]["matchLabels"]
    for policy in policies:
        meta, spec = policy.get("metadata", {}), policy.get("spec", {})
        if policy.get("kind") != "NetworkPolicy" or meta.get("namespace") != OPERATOR_NAMESPACE:
            raise ValueError("unexpected policy readback")
        if meta.get("name") == expected["metadata"]["name"]:
            if (not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp")
                    or {**spec, "ingress": spec.get("ingress", [])} != expected["spec"]):
                raise ValueError("quarantine is missing, changed or being deleted")
            found += 1
        elif spec.get("ingress") and _may_select(spec.get("podSelector"), labels):
            raise ValueError("another ingress policy may open the quarantined proxy")
    if found != 1:
        raise ValueError("exactly one applied quarantine policy required")


def routes(side, metrics_fqdn, readback):
    """One ProxyGroup per side so the tailnet sees the Collector, not the node or another workload."""
    verify_quarantine(side, readback)
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
    parser.add_argument("stage", choices=["proxy-quarantine", "routes", "collector-egress", "proxy-ingress"])
    parser.add_argument("--side", required=True, choices=SIDES)
    parser.add_argument("--policy-readback", type=Path, help="fresh full tailscale-system NetworkPolicy list")
    parser.add_argument("--metrics-fqdn", help="independently verified bravo-metrics.<tailnet>.ts.net")
    parser.add_argument("--transport-port", type=int, action="append",
                        help="current proxy UDP listen port; repeat for a second address family")
    args = parser.parse_args(argv)
    if args.stage == "proxy-quarantine":
        result = proxy_quarantine(args.side)
    elif args.stage == "routes":
        if args.metrics_fqdn is None or args.policy_readback is None:
            parser.error("routes requires --metrics-fqdn and --policy-readback")
        result = {"apiVersion": "v1", "kind": "List", "items": routes(
            args.side, args.metrics_fqdn, json.loads(args.policy_readback.read_text()))}
    elif args.stage == "collector-egress":
        result = collector_egress(args.side, json.load(sys.stdin))
    else:
        if args.transport_port is None:
            parser.error("proxy-ingress requires --transport-port from current Pod-bound readback")
        result = proxy_ingress(args.side, json.load(sys.stdin), transport_ports=args.transport_port)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
