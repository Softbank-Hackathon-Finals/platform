#!/usr/bin/env python3
"""Render explicitly separated T8 tailnet routes; never register devices or set ACLs."""
import argparse
import json
import re

CHART_VERSION = "1.104.4"
CHART_SHA256 = "971ff11ecc285613cd29d6f48a8ada180d7bdf716ac3301a1e663837f6419998"
# v1.104.4 multi-arch index digests (amd64·arm64·arm·386). The earlier pins were the amd64 manifests inside
# them, so the operator could not start on an arm64 node ("exec format error", T49 bravo-t49, 11/8).
OPERATOR_DIGEST = "sha256:68fc0aa4e1c3d7ba19ca41c0bc27f17b2a5a8dca6c10efcdd18020a6bf97768d"
PROXY_DIGEST = "sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c"
# Source (EKS, amd64) runs the amd64 images of the same v1.104.4 indexes (checked 2026-11-08). Its operator is
# taken over by GitOps as it runs: another image string rolls every source proxy (DB exposure, replication),
# so moving it to the index digests is its own reviewed change.
SOURCE_DIGESTS = {"operator": "sha256:48d373932bae9b65f5ded6480276b2290f16b8d43794ec9dcc3faf50137802eb",
                  "proxy": "sha256:519bf6f2c386992052216ea7e9391d5ace3a859d458f4f61bc8db8c58377eb7d"}


ENV = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
AUDIENCE = re.compile(r"^[A-Za-z0-9:/._-]{1,256}$")


def values(side, env=None, auth="join", client_id="", audience=""):
    """Operator chart values. `env` names the tailnet tags (tag:bravo-<env>-operator, ADR-0035); it
    defaults to the side, which keeps the existing target-dev and source-dev tags.

    auth "join": the operator reads a precreated operator-oauth Secret (existing environments).
    auth "wif": workload identity federation. The chart mints a projected ServiceAccount token with
    `audience` and exchanges it for the federated identity `client_id`; no Secret exists.
    """
    if side not in {"source", "target"}:
        raise ValueError("source or target required")
    env = env or side
    if not ENV.fullmatch(env):
        raise ValueError("env must be a lowercase environment name")
    if auth == "wif":
        # The stack passes "-" for an unset parameter.
        if client_id == "-" or audience == "-" or not (CLIENT_ID.fullmatch(client_id or "")
                                                        and AUDIENCE.fullmatch(audience or "")):
            raise ValueError("wif needs the federated identity client_id and audience")
        oauth = {"clientId": client_id, "audience": audience}
    elif auth == "join":
        oauth = {"clientId": "", "clientSecret": ""}
    else:
        raise ValueError("auth must be join or wif")
    return {
        "oauth": oauth,
        "operatorConfig": {
            "defaultTags": [f"tag:bravo-{env}-operator"],
            "hostname": f"bravo-{env}-operator",
            "image": {"repository": "tailscale/k8s-operator",
                      "digest": SOURCE_DIGESTS["operator"] if side == "source" else OPERATOR_DIGEST,
                      "pullPolicy": "IfNotPresent"},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}},
        },
        "proxyConfig": {
            "image": {"repository": "tailscale/tailscale",
                      "digest": SOURCE_DIGESTS["proxy"] if side == "source" else PROXY_DIGEST},
            "defaultTags": f"tag:bravo-{env}-db",
        },
        # Source: a transport proxy (source-dev `api-transport-access`). It passes the caller's own credential
        # (an EKS IAM token) through and never impersonates, so no new RBAC is made. Target: a wif destination is
        # a cluster the platform built (T45); the platform's Executor reaches its API through this proxy in auth
        # mode (tailnet identity -> RBAC, never noauth). Existing joined targets keep it off (operator-access).
        # allowImpersonation (source): the impersonation RBAC for the separate auth-mode kube-apiserver ProxyGroup
        # platform workloads use (source-dev `workload-api-access`); the built-in source proxy stays noauth.
        "apiServerProxyConfig": {"mode": "noauth" if side == "source" else ("true" if auth == "wif" else "false"),
                                 "allowImpersonation": "true" if side == "source" else "false"},
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
    parser.add_argument("--env", help="tailnet environment name for tags (default: the side)")
    parser.add_argument("--auth", default="join", choices=["join", "wif"])
    parser.add_argument("--client-id", default="")
    parser.add_argument("--audience", default="")
    args = parser.parse_args()
    if args.stage in MOVED_TO_IAC:
        parser.exit(2, f"{args.stage} no longer renders this route: it is declared in infra/iac/environments "
                       "(tailnet-ingress/tailnet-egress, T44, ADR-0032); render it with `bravo-iac chart`.\n")
    if args.stage == "values":
        result = values(args.side, args.env, args.auth, args.client_id, args.audience)
    else:
        items = [ingress(args.side)] if args.stage == "ingress" else egress(args.side, args.peer_fqdn)
        result = {"apiVersion": "v1", "kind": "List", "items": items}
    print(json.dumps(result, indent=2))
