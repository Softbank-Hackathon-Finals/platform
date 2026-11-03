#!/usr/bin/env python3
"""Render the source pooler's dedicated target DB route without switching a backend."""
import argparse
import json
import re
import sys

from control_network import observed_proxy_ports, proxy_ingress_policy

GROUP = "bravo-source-pooler"
NAMESPACE = "source-dev"
SERVICE = "target-db-for-pooler"
CALLER = {"app": "source-pgbouncer"}


def egress(target_db_fqdn):
    """Use the existing T8 ProxyGroup/ExternalName contract with a distinct identity."""
    if not isinstance(target_db_fqdn, str) or not re.fullmatch(
        r"bravo-target-db\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net", target_db_fqdn
    ):
        raise ValueError("verified target DB's exact MagicDNS FQDN required")
    return [
        {"apiVersion": "tailscale.com/v1alpha1", "kind": "ProxyGroup",
         "metadata": {"name": GROUP, "labels": {"component": "source"}},
         "spec": {"type": "egress", "replicas": 1, "tags": ["tag:" + GROUP]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": SERVICE, "namespace": NAMESPACE,
            "labels": {"component": "source", "app.kubernetes.io/part-of": "bravo-pooler-backend"},
            "annotations": {"tailscale.com/tailnet-fqdn": target_db_fqdn,
                            "tailscale.com/proxy-group": GROUP},
        }, "spec": {"type": "ExternalName", "externalName": "placeholder",
                    "ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}]}},
    ]


def proxy_ingress(observed_services, *, transport_ports):
    ports = observed_proxy_ports(observed_services, group=GROUP, namespace=NAMESPACE,
                                 expected={SERVICE: ("postgres", 5432)})
    policy = proxy_ingress_policy(ports, group=GROUP, namespace=NAMESPACE, caller=CALLER,
                                  transport_ports=transport_ports,
                                  policy_name="source-pooler-backend-ingress")
    policy["metadata"]["labels"]["component"] = "source"
    policy["metadata"]["labels"]["app.kubernetes.io/part-of"] = "bravo-pooler-backend"
    return policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", default="routes", choices=["routes", "proxy-ingress"])
    parser.add_argument("--target-db-fqdn",
                        help="independently verified bravo-target-db.<tailnet>.ts.net")
    parser.add_argument("--transport-port", type=int, action="append",
                        help="current proxy UDP listen port; repeat for a second address family")
    args = parser.parse_args(argv)
    if args.stage == "routes":
        if args.target_db_fqdn is None:
            parser.error("routes requires --target-db-fqdn")
        result = {"apiVersion": "v1", "kind": "List", "items": egress(args.target_db_fqdn)}
    else:
        if args.transport_port is None:
            parser.error("proxy-ingress requires --transport-port from current Pod-bound readback")
        result = proxy_ingress(json.load(sys.stdin), transport_ports=args.transport_port)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
