"""NetworkPolicy for the T31 apply from the T10 workflow host (infra/charts/workflow-host).

Same shape as rehearsal-access/rehearsal-host-network.yaml; the operator applies it with the other
T7 tailnet policies (GitOps does not manage NetworkPolicy). platform-state is default-deny. Only the
host Pod that mounts a target kubeconfig (label bravo.dev/target-api-client=apply, set by the chart
only when application.targetKubeconfigSecret is set) gets:
- DNS;
- the target Kubernetes API, and nothing else on it: either the Tailscale egress ProxyGroup pods on
  TCP 443 (--tailscale-proxy, plus the matching ingress on the proxy) or one registered CIDR and port
  (--target-cidr/--target-port, e.g. a directly routed API endpoint).

    python3 infra/envs/apply-host/network.py --tailscale-proxy bravo-rehearsal-egress | kubectl apply --dry-run=server -f -
"""

import argparse
import ipaddress
import json
import re
import sys

NAMESPACE = "platform-state"
HOST = {"app.kubernetes.io/name": "t10-workflow-host", "bravo.dev/target-api-client": "apply"}
LABELS = {"app.kubernetes.io/name": "t10-workflow-host", "component": "platform", "bravo.dev/owner": "t31-host"}
DNS = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
               "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
       "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}


def _proxy(name):
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
        raise ValueError("invalid_proxy_group")
    return {"tailscale.com/managed": "true", "tailscale.com/parent-resource": name,
            "tailscale.com/parent-resource-type": "proxygroup"}


def _policy(name, namespace, spec):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace, "labels": LABELS}, "spec": spec}


def render(*, tailscale_proxy=None, target_cidr=None, target_port=None):
    if (tailscale_proxy is None) == (target_cidr is None) or (target_cidr is None) != (target_port is None):
        raise ValueError("one_target_route_required")
    if tailscale_proxy is not None:
        proxy = _proxy(tailscale_proxy)
        route = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tailscale-system"}},
                         "podSelector": {"matchLabels": proxy}}],
                 "ports": [{"port": 443, "protocol": "TCP"}]}
    else:
        network = ipaddress.ip_network(target_cidr)
        if network.version != 4 or network.prefixlen < 24 or type(target_port) is not int or not 1 <= target_port <= 65535:
            raise ValueError("narrow_ipv4_target_required")
        route = {"to": [{"ipBlock": {"cidr": str(network)}}], "ports": [{"port": target_port, "protocol": "TCP"}]}
    items = [_policy("t10-workflow-host-target-api", NAMESPACE,
                     {"podSelector": {"matchLabels": HOST}, "policyTypes": ["Egress"], "egress": [DNS, route]})]
    if tailscale_proxy is not None:
        items.append(_policy("t10-workflow-host-proxy-ingress", "tailscale-system", {
            "podSelector": {"matchLabels": proxy}, "policyTypes": ["Ingress"], "ingress": [{
                "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                          "podSelector": {"matchLabels": HOST}}],
                "ports": [{"port": 443, "protocol": "TCP"}]}]}))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def _check():
    out = render(tailscale_proxy="bravo-rehearsal-egress")
    egress = out["items"][0]["spec"]["egress"]
    assert out["items"][0]["spec"]["podSelector"]["matchLabels"] == HOST
    assert egress[0] == DNS and egress[1]["ports"] == [{"port": 443, "protocol": "TCP"}]
    assert all(peer.get("namespaceSelector") for rule in egress for peer in rule["to"])  # never a bare podSelector
    assert out["items"][1]["metadata"]["namespace"] == "tailscale-system"
    assert out["items"][1]["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == HOST
    direct = render(target_cidr="172.18.0.2/32", target_port=6443)
    assert len(direct["items"]) == 1 and direct["items"][0]["spec"]["egress"][1]["to"] == [{"ipBlock": {"cidr": "172.18.0.2/32"}}]
    for bad in ({}, {"tailscale_proxy": "p", "target_cidr": "10.0.0.1/32", "target_port": 1},
                {"target_cidr": "0.0.0.0/0", "target_port": 443}, {"target_cidr": "10.0.0.1/32"},
                {"tailscale_proxy": "Bad_Name"}):
        try:
            render(**bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _check()
        print("ok")
    else:
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        route = parser.add_mutually_exclusive_group(required=True)
        route.add_argument("--tailscale-proxy", help="ProxyGroup name (tailscale.com/parent-resource)")
        route.add_argument("--target-cidr", help="registered target API address, IPv4 /24 or narrower")
        parser.add_argument("--target-port", type=int)
        args = parser.parse_args()
        json.dump(render(tailscale_proxy=args.tailscale_proxy, target_cidr=args.target_cidr,
                         target_port=args.target_port), sys.stdout, indent=1)
