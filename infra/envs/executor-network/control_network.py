#!/usr/bin/env python3
"""Render dedicated Executor tailnet paths; never register nodes or change ACLs."""
import argparse
import json
import re
import sys
from uuid import UUID

CHART_VERSION = "1.104.4"
CHART_SHA256 = "971ff11ecc285613cd29d6f48a8ada180d7bdf716ac3301a1e663837f6419998"
OPERATOR_DIGEST = "sha256:48d373932bae9b65f5ded6480276b2290f16b8d43794ec9dcc3faf50137802eb"
PROXY_DIGEST = "sha256:519bf6f2c386992052216ea7e9391d5ace3a859d458f4f61bc8db8c58377eb7d"
# v1.104.4 commit 27047635f237d1f3e0b08c0f487674757f30d2f7:
# egress-services.go unusedPort() uses rand.Int32N(maxPorts) + 10000;
# maxPorts is 1000. Its earlier reconciler comment still names an old range.
PROXY_PORT_MIN = 10000
PROXY_PORT_LIMIT = 11000
NAMESPACE = "bravo-platform"
OPERATOR_NAMESPACE = "tailscale-system"
GROUP = "bravo-cutover-control"
SIDES = ("source", "target")
CALLER = {"app.kubernetes.io/name": "cutover-executor"}
# Tailscale v1.104.4 proxygroup_specs.go pgLabels(), applied to proxy Pods.
PROXY_LABELS = {"tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "proxygroup",
                "tailscale.com/parent-resource": GROUP}


def obj(kind, name, spec, namespace=NAMESPACE, component="platform", api="v1", annotations=None):
    metadata = {"name": name, "labels": {"component": component,
                "app.kubernetes.io/part-of": "bravo-cutover-network"}}
    if namespace:
        metadata["namespace"] = namespace
    if annotations:
        metadata["annotations"] = annotations
    return {"apiVersion": api, "kind": kind, "metadata": metadata, "spec": spec}


def values():
    return {
        "oauth": {"clientId": "", "clientSecret": ""},
        "oauthSecretVolume": {"secret": {"secretName": "operator-oauth"}},
        "operatorConfig": {
            "defaultTags": ["tag:bravo-platform-operator"], "hostname": "bravo-platform-operator",
            "image": {"repository": "tailscale/k8s-operator", "digest": OPERATOR_DIGEST,
                      "pullPolicy": "IfNotPresent"},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}},
        },
        "proxyConfig": {"image": {"repository": "tailscale/tailscale", "digest": PROXY_DIGEST},
                        "defaultTags": "tag:" + GROUP},
        "apiServerProxyConfig": {"mode": "false", "allowImpersonation": "false"},
        "ingressClass": {"enabled": False, "name": "bravo-cutover-unused"},
    }


def route_contract(include_api=False):
    if type(include_api) is not bool:
        raise ValueError("include_api must be an explicit boolean")
    kinds = [("db", "db", 5432), ("pooler", "control-pooler", 6432)]
    if include_api:
        kinds.append(("api", "operator", 443))
    return [(side, kind, host, port) for side in SIDES for kind, host, port in kinds]


def routes(fqdns, include_api=False):
    contract = route_contract(include_api)
    expected = {f"{side}_{kind}" for side, kind, _, _ in contract}
    if not isinstance(fqdns, dict) or set(fqdns) != expected:
        raise ValueError("provide exactly the selected source/target route FQDNs")
    suffixes = set()
    for side, kind, host, _ in contract:
        name = fqdns[f"{side}_{kind}"]
        match = re.fullmatch(rf"bravo-{side}-{host}\.([a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?)\.ts\.net", name) if isinstance(name, str) else None
        if match is None:
            raise ValueError("verified exact side, service and MagicDNS FQDN required")
        suffixes.add(match.group(1))
    if len(suffixes) != 1:
        raise ValueError("all routes must belong to the same verified tailnet")
    items = [obj("ProxyGroup", GROUP, {"type": "egress", "replicas": 1, "tags": ["tag:" + GROUP]},
                 namespace=None, api="tailscale.com/v1alpha1")]
    for side, kind, _, port in contract:
        items.append(obj("Service", f"{side}-{kind}-for-cutover", {
            "type": "ExternalName", "externalName": "placeholder",
            "ports": [{"name": kind, "port": port, "protocol": "TCP"}],
        }, annotations={"tailscale.com/tailnet-fqdn": fqdns[f"{side}_{kind}"],
                        "tailscale.com/proxy-group": GROUP}))
    return items


def pooler_ingress(side):
    if side not in SIDES:
        raise ValueError("source or target required")
    return obj("Service", f"{side}-control-pooler-tailnet", {
        "selector": {"app": f"{side}-pgbouncer"},
        "ports": [{"name": "pooler", "port": 6432, "targetPort": 5432, "protocol": "TCP"}],
    }, namespace=f"{side}-dev", component=side, annotations={
        "tailscale.com/expose": "true", "tailscale.com/hostname": f"bravo-{side}-control-pooler",
        "tailscale.com/tags": f"tag:bravo-{side}-control-pooler",
    })


def observed_proxy_ports(observed_services, *, group, namespace, expected):
    """Bind exact proxy ports to the operator's generated child Service readback."""
    if isinstance(observed_services, dict) and observed_services.get("kind") in {"List", "ServiceList"}:
        observed_services = observed_services.get("items")
    if not isinstance(observed_services, list):
        raise TypeError("current operator namespace Service List required")
    seen, uids, ports = set(), set(), set()
    for service in observed_services:
        if not isinstance(service, dict):
            raise TypeError("Service objects are required")
        metadata = service.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get("tailscale.com/proxy-group") != group:
            continue
        parent = labels.get("tailscale.com/parent-resource")
        if parent not in expected or parent in seen:
            raise ValueError("unexpected or duplicate cutover child Service")
        wanted = {"tailscale.com/managed": "true", "tailscale.com/parent-resource-type": "svc",
                  "tailscale.com/parent-resource-ns": namespace, "tailscale.com/svc-type": "egress"}
        if any(labels.get(k) != v for k, v in wanted.items()):
            raise ValueError("cutover child Service ownership labels changed")
        spec = service.get("spec", {})
        if (service.get("kind") != "Service" or service.get("apiVersion") != "v1"
                or metadata.get("namespace") != OPERATOR_NAMESPACE or not metadata.get("name")
                or not metadata.get("resourceVersion") or spec.get("type") != "ClusterIP"
                or spec.get("selector")):
            raise ValueError("current generated ClusterIP Service metadata required")
        try:
            uid = UUID(metadata.get("uid", ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("generated Service UID required") from None
        if uid in uids:
            raise ValueError("generated Service UID reused")
        kind, remote = expected[parent]
        mappings = []
        for mapping in spec.get("ports", []):
            if mapping.get("name") == "tailscale-health-check":
                if mapping.get("targetPort") != 9002 or mapping.get("protocol") != "TCP":
                    raise ValueError("unexpected operator health port")
                continue
            if mapping.get("name") != kind or mapping.get("port") != remote or mapping.get("protocol") != "TCP":
                raise ValueError("child Service remote port differs from the route contract")
            target = mapping.get("targetPort")
            if type(target) is not int or not PROXY_PORT_MIN <= target < PROXY_PORT_LIMIT or target in ports:
                raise ValueError("invalid or reused proxy target port")
            mappings.append(target)
        if len(mappings) != 1:
            raise ValueError("exactly one business port mapping is required per route")
        ports.update(mappings)
        seen.add(parent)
        uids.add(uid)
    if seen != set(expected):
        raise ValueError("one or more selected route mappings are missing")
    # Cover service-port and post-DNAT Pod-port enforcement; both are restricted
    # to this exact ProxyGroup. Never open the entire allocation range or 9002.
    return sorted(ports | {remote for _, remote in expected.values()})


def mapped_ports(observed_services, include_api=False):
    expected = {f"{side}-{kind}-for-cutover": (kind, port)
                for side, kind, _, port in route_contract(include_api)}
    return observed_proxy_ports(observed_services, group=GROUP, namespace=NAMESPACE, expected=expected)


def network(observed_services, include_api=False):
    ports = mapped_ports(observed_services, include_api)
    return obj("NetworkPolicy", "cutover-executor-tailnet", {
        "podSelector": {"matchLabels": CALLER.copy()}, "policyTypes": ["Egress"],
        "egress": [{"to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": OPERATOR_NAMESPACE}},
            "podSelector": {"matchLabels": PROXY_LABELS.copy()},
        }], "ports": [{"protocol": "TCP", "port": port} for port in ports]}],
    }, api="networking.k8s.io/v1")


def proxy_ingress_policy(ports, *, group, namespace, caller, transport_ports, policy_name):
    """Isolate this ProxyGroup using its verified current UDP listen ports."""
    if (not isinstance(transport_ports, list) or not 1 <= len(transport_ports) <= 2
            or any(type(port) is not int or not 1 <= port <= 65535 for port in transport_ports)):
        raise ValueError("one or two verified current proxy WireGuard listen ports are required")
    if len(set(transport_ports)) != len(transport_ports):
        raise ValueError("transport ports must be unique")
    if set(transport_ports) & (set(ports) | {443, 5432, 6432, 9002}):
        raise ValueError("transport port conflicts with a reserved business or health port")
    labels = {**PROXY_LABELS, "tailscale.com/parent-resource": group}
    return obj("NetworkPolicy", policy_name, {
        "podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress"],
        "ingress": [
            {"from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
                "podSelector": {"matchLabels": caller.copy()},
            }], "ports": [{"protocol": "TCP", "port": port} for port in ports]},
            {"from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": OPERATOR_NAMESPACE}},
                "podSelector": {"matchLabels": {"app": "operator"}},
            }], "ports": [{"protocol": "TCP", "port": 9002}]},
            # WireGuard authenticates encrypted transport independently of the
            # cleartext business and operator health routes above.
            {"ports": [{"protocol": "UDP", "port": port} for port in sorted(transport_ports)]},
        ],
    }, namespace=OPERATOR_NAMESPACE, api="networking.k8s.io/v1")


def proxy_ingress(observed_services, include_api=False, *, transport_ports):
    return proxy_ingress_policy(mapped_ports(observed_services, include_api), group=GROUP,
                                namespace=NAMESPACE, caller=CALLER, transport_ports=transport_ports,
                                policy_name="cutover-proxy-ingress")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["values", "routes", "pooler-ingress", "network", "proxy-ingress"])
    parser.add_argument("--side", choices=SIDES)
    parser.add_argument("--include-api", action="store_true", help="only after separate HTTPS/API authorization verification")
    parser.add_argument("--transport-port", type=int, action="append",
                        help="verified current proxy UDP listen port; repeat for a second address family")
    for side, kind, _, _ in route_contract(True):
        parser.add_argument(f"--{side}-{kind}-fqdn")
    args = parser.parse_args(argv)
    if args.stage == "proxy-ingress" and args.transport_port is None:
        parser.error("proxy-ingress requires --transport-port from current Pod-bound readback")
    if args.stage == "values":
        result = values()
    elif args.stage == "pooler-ingress":
        result = pooler_ingress(args.side)
    elif args.stage == "routes":
        fqdns = {f"{side}_{kind}": value for side, kind, _, _ in route_contract(True)
                 if (value := getattr(args, f"{side}_{kind}_fqdn")) is not None}
        result = {"apiVersion": "v1", "kind": "List", "items": routes(fqdns, args.include_api)}
    elif args.stage == "network":
        result = network(json.load(sys.stdin), args.include_api)
    else:
        result = proxy_ingress(json.load(sys.stdin), args.include_api, transport_ports=args.transport_port)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
