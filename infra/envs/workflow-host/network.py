"""NetworkPolicies for the T10 workflow host (infra/charts/workflow-host); applied by the operator.

platform-state is default-deny and GitOps does not manage NetworkPolicy. The host's DB egress comes
from the existing runtime-database policy (bravo.dev/db-client=runtime). This adds only:
- DNS; the Cutover Executor read API (bravo-platform/cutover-executor, TCP 8080, HTTPS);
- GitHub over TCP 443 to the published api/git/web IPv4 ranges (GitHub App writer, T7 run reads,
  demo-chat object fetch, the relay's public fence re-check). No other internet egress.
- the Executor's ingress from this host only.

    curl -fsS https://api.github.com/meta > meta.json   # public, no credential
    python3 infra/envs/workflow-host/network.py --github-meta meta.json | kubectl apply --dry-run=server -f -
"""

import argparse
import ipaddress
import json
import sys

HOST = {"app.kubernetes.io/name": "t10-workflow-host", "component": "platform"}
EXECUTOR = {"app.kubernetes.io/name": "cutover-executor"}


def _ns(name):
    return {"matchLabels": {"kubernetes.io/metadata.name": name}}


def github_ranges(meta):
    ranges = sorted({r for key in ("api", "git", "web") for r in meta.get(key, [])
                     if ipaddress.ip_network(r).version == 4})
    if not ranges or len(ranges) > 200:
        raise ValueError("github_meta_ranges_unexpected")
    return ranges


def render(meta):
    labels = {"app.kubernetes.io/name": "t10-workflow-host", "component": "platform"}
    egress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "t10-workflow-host-egress", "namespace": "platform-state", "labels": labels},
              "spec": {"podSelector": {"matchLabels": HOST}, "policyTypes": ["Egress"], "egress": [
                  {"to": [{"namespaceSelector": _ns("kube-system"), "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                   "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
                  {"to": [{"namespaceSelector": _ns("bravo-platform"), "podSelector": {"matchLabels": EXECUTOR}}],
                   "ports": [{"port": 8080, "protocol": "TCP"}]},
                  {"to": [{"ipBlock": {"cidr": r}} for r in github_ranges(meta)],
                   "ports": [{"port": 443, "protocol": "TCP"}]}]}}
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "cutover-executor-from-workflow-host", "namespace": "bravo-platform",
                            "labels": labels},
               "spec": {"podSelector": {"matchLabels": EXECUTOR}, "policyTypes": ["Ingress"], "ingress": [
                   {"from": [{"namespaceSelector": _ns("platform-state"), "podSelector": {"matchLabels": HOST}}],
                    "ports": [{"port": 8080, "protocol": "TCP"}]}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [egress, ingress]}


def _check():
    out = render({"api": ["140.82.112.0/20", "2a0a:a440::/29"], "git": ["140.82.112.0/20"], "web": ["192.30.252.0/22"]})
    egress = out["items"][0]["spec"]["egress"]
    assert [b["ipBlock"]["cidr"] for b in egress[2]["to"]] == ["140.82.112.0/20", "192.30.252.0/22"]  # IPv4, deduped
    assert egress[1]["ports"] == [{"port": 8080, "protocol": "TCP"}]
    assert all(peer.get("namespaceSelector") for rule in egress[:2] for peer in rule["to"])  # never a bare podSelector
    assert out["items"][1]["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == HOST
    try:
        render({})
        raise AssertionError("empty meta accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _check()
        print("ok")
    else:
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--github-meta", required=True)
        args = parser.parse_args()
        with open(args.github_meta) as stream:
            json.dump(render(json.load(stream)), sys.stdout, indent=1)
