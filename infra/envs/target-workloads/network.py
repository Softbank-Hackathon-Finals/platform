#!/usr/bin/env python3
"""Render explicitly separated T8 tailnet routes; never register devices or set ACLs."""
import argparse
import json
import re

CHART_VERSION = "1.104.4"
CHART_SHA256 = "971ff11ecc285613cd29d6f48a8ada180d7bdf716ac3301a1e663837f6419998"
OPERATOR_DIGEST = "sha256:48d373932bae9b65f5ded6480276b2290f16b8d43794ec9dcc3faf50137802eb"
PROXY_DIGEST = "sha256:519bf6f2c386992052216ea7e9391d5ace3a859d458f4f61bc8db8c58377eb7d"


def values(side):
    if side not in {"source", "target"}:
        raise ValueError("source or target required")
    return {
        "oauth": {"clientId": "", "clientSecret": ""},
        "operatorConfig": {
            "defaultTags": [f"tag:bravo-{side}-operator"],
            "hostname": f"bravo-{side}-operator",
            "image": {"repository": "tailscale/k8s-operator", "digest": OPERATOR_DIGEST,
                      "pullPolicy": "IfNotPresent"},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}},
        },
        "proxyConfig": {
            "image": {"repository": "tailscale/tailscale", "digest": PROXY_DIGEST},
            "defaultTags": f"tag:bravo-{side}-db",
        },
        "apiServerProxyConfig": {"mode": "false", "allowImpersonation": "false"},
        "ingressClass": {"enabled": False, "name": "bravo-t8-unused"},
    }


def ingress(side):
    values(side)
    return {"apiVersion": "v1", "kind": "Service", "metadata": {
        "name": f"{side}-db-tailnet", "namespace": f"{side}-dev",
        "labels": {"component": side, "app.kubernetes.io/part-of": "bravo-t8-network"},
        "annotations": {"tailscale.com/expose": "true",
                        "tailscale.com/hostname": f"bravo-{side}-db",
                        "tailscale.com/tags": f"tag:bravo-{side}-db"},
    }, "spec": {"selector": {"cnpg.io/cluster": f"{side}-db", "cnpg.io/instanceRole": "primary"},
                "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432,
                           "protocol": "TCP"}]}}


def egress(side, peer_fqdn):
    values(side)
    peer = "target" if side == "source" else "source"
    if not isinstance(peer_fqdn, str) or not re.fullmatch(
        rf"bravo-{peer}-db\.[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.ts\.net", peer_fqdn
    ):
        raise ValueError("verified peer DB's exact MagicDNS FQDN required")
    routes = ["replication"] + (["pooler"] if side == "target" else [])
    result = []
    for route in routes:
        name = f"bravo-{side}-{route}"
        result.extend([
            {"apiVersion": "tailscale.com/v1alpha1", "kind": "ProxyGroup",
             "metadata": {"name": name, "labels": {"component": side}},
             "spec": {"type": "egress", "replicas": 1, "tags": [f"tag:{name}"]}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {
                "name": f"{peer}-db-for-{route}", "namespace": f"{side}-dev",
                "labels": {"component": side, "app.kubernetes.io/part-of": "bravo-t8-network"},
                "annotations": {"tailscale.com/tailnet-fqdn": peer_fqdn,
                                "tailscale.com/proxy-group": name},
            }, "spec": {"type": "ExternalName", "externalName": "placeholder",
                        "ports": [{"name": "postgres", "port": 5432, "protocol": "TCP"}]}},
        ])
    return result


# The DB devices and the replication/pooler egress routes are owned by the tailnet-ingress and
# tailnet-egress IaC modules (infra/iac/environments/{source,target}-dev.json).
MOVED_TO_IAC = {"ingress", "egress"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["values", "ingress", "egress"])
    parser.add_argument("--side", required=True, choices=["source", "target"])
    parser.add_argument("--peer-fqdn")
    args = parser.parse_args()
    if args.stage in MOVED_TO_IAC:
        parser.exit(2, f"{args.stage} no longer renders this route: it is declared in infra/iac/environments "
                       "(tailnet-ingress/tailnet-egress, T44, ADR-0032); render it with `bravo-iac chart`.\n")
    if args.stage == "values":
        result = values(args.side)
    else:
        items = [ingress(args.side)] if args.stage == "ingress" else egress(args.side, args.peer_fqdn)
        result = {"apiVersion": "v1", "kind": "List", "items": items}
    print(json.dumps(result, indent=2))
