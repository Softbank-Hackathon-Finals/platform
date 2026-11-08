"""NetworkPolicies for the T10 workflow host (infra/charts/workflow-host); applied by the operator.

platform-state is default-deny and GitOps does not manage NetworkPolicy. The host's DB egress comes
from the existing runtime-database policy (bravo.dev/db-client=runtime). This adds only:
- DNS; the Cutover Executor read API (bravo-platform/cutover-executor, TCP 8080, HTTPS);
- GitHub over TCP 443 to the published api/git/web IPv4 ranges (GitHub App writer, T7 run reads,
  demo-chat object fetch, the relay's public fence re-check). No other internet egress.
- the Executor's ingress from this host only.
- with --aws-endpoints (ADR-0038, T19 platform signer): TCP 443 to the private IPs of the platform VPC's
  STS and KMS interface endpoints only (infra/envs/platform-gate-signer output `endpoint_ips`).

- with --kube-api (T49: the host creates the initial-proposal and platform load Jobs in bravo-initial,
  initialProposalAuto): a separate policy `t10-workflow-host-kube-api`, TCP to the API server endpoint /32s only.
  It is its own object so applying it never rewrites the egress policy above (or rules others added to it).

    kubectl get endpointslice -n default -l kubernetes.io/service-name=kubernetes   # the endpoint ip:port
    python3 infra/envs/workflow-host/network.py --kube-api 10.0.0.10:6443 | kubectl apply -f -

- with --artifact-storage (T49: the host reads T7 candidate artifacts itself; GitHub answers an artifact
  download with a redirect to Azure Blob storage, outside the GitHub meta ranges): a separate policy
  `t10-workflow-host-artifact-storage`, TCP 443 to the resolved public /32s of that one storage host only.
  ponytail: resolved addresses, not a name rule (the CNI has no FQDN policy); re-render if the host moves.

    dig +short productionresultssa17.blob.core.windows.net   # the Location host of an artifact download
    python3 infra/envs/workflow-host/network.py --artifact-storage 20.150.82.228 --artifact-storage 52.239.172.36 | kubectl apply -f -

The T31 apply route (host -> bravo-apply-egress proxies -> target API) and the proxies' ingress from
this host are declared in infra/iac/environments/platform.json (tailnet-egress, T44), not here.

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


def endpoint_ips(ips):
    """The STS/KMS interface endpoint ENIs: private IPv4 /32s only, never a public range."""
    out = sorted({str(ipaddress.ip_address(ip)) for ip in ips})
    if not out or len(out) > 12 or not all(ipaddress.ip_address(ip).is_private for ip in out):
        raise ValueError("aws_endpoint_ips_unexpected")
    return out


def render(meta, aws_endpoints=None):
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
    if aws_endpoints is not None:
        egress["spec"]["egress"].append({"to": [{"ipBlock": {"cidr": ip + "/32"}} for ip in endpoint_ips(aws_endpoints)],
                                         "ports": [{"port": 443, "protocol": "TCP"}]})
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "cutover-executor-from-workflow-host", "namespace": "bravo-platform",
                            "labels": labels},
               "spec": {"podSelector": {"matchLabels": EXECUTOR}, "policyTypes": ["Ingress"], "ingress": [
                   {"from": [{"namespaceSelector": _ns("platform-state"), "podSelector": {"matchLabels": HOST}}],
                    "ports": [{"port": 8080, "protocol": "TCP"}]}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [egress, ingress]}


def kube_api(endpoints):
    """Egress to the Kubernetes API server endpoints (private ip:port), as its own NetworkPolicy."""
    peers = sorted({(str(ipaddress.ip_address(e.rsplit(":", 1)[0])), int(e.rsplit(":", 1)[1])) for e in endpoints})
    if not peers or len(peers) > 6 or not all(ipaddress.ip_address(ip).is_private and 0 < port < 65536
                                             for ip, port in peers):
        raise ValueError("kube_api_endpoints_unexpected")
    labels = {"app.kubernetes.io/name": "t10-workflow-host", "component": "platform"}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "t10-workflow-host-kube-api", "namespace": "platform-state", "labels": labels},
         "spec": {"podSelector": {"matchLabels": HOST}, "policyTypes": ["Egress"], "egress": [
             {"to": [{"ipBlock": {"cidr": ip + "/32"}}], "ports": [{"port": port, "protocol": "TCP"}]}
             for ip, port in peers]}}]}


def artifact_storage(ips):
    """Egress to the artifact storage host's public addresses (TCP 443), as its own NetworkPolicy."""
    out = sorted({str(ipaddress.ip_address(ip)) for ip in ips})
    if not out or len(out) > 8 or any(ipaddress.ip_address(ip).is_private for ip in out):
        raise ValueError("artifact_storage_ips_unexpected")
    labels = {"app.kubernetes.io/name": "t10-workflow-host", "component": "platform"}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "t10-workflow-host-artifact-storage", "namespace": "platform-state", "labels": labels},
         "spec": {"podSelector": {"matchLabels": HOST}, "policyTypes": ["Egress"], "egress": [
             {"to": [{"ipBlock": {"cidr": ip + "/32"}} for ip in out], "ports": [{"port": 443, "protocol": "TCP"}]}]}}]}


def _check():
    blob = artifact_storage(["52.239.172.36", "20.150.82.228", "52.239.172.36"])["items"][0]["spec"]["egress"]
    assert blob == [{"to": [{"ipBlock": {"cidr": "20.150.82.228/32"}}, {"ipBlock": {"cidr": "52.239.172.36/32"}}],
                     "ports": [{"port": 443, "protocol": "TCP"}]}]
    try:
        artifact_storage(["10.0.0.1"])
        raise AssertionError("private artifact address accepted")
    except ValueError:
        pass
    api = kube_api(["10.81.0.191:6443", "10.81.0.191:6443"])["items"][0]
    assert api["metadata"]["name"] == "t10-workflow-host-kube-api" and api["spec"]["egress"] == [
        {"to": [{"ipBlock": {"cidr": "10.81.0.191/32"}}], "ports": [{"port": 6443, "protocol": "TCP"}]}]
    for bad in ([], ["52.95.1.1:443"]):
        try:
            kube_api(bad)
            raise AssertionError("public or empty API endpoint accepted")
        except ValueError:
            pass
    out = render({"api": ["140.82.112.0/20", "2a0a:a440::/29"], "git": ["140.82.112.0/20"], "web": ["192.30.252.0/22"]})
    egress = out["items"][0]["spec"]["egress"]
    assert [b["ipBlock"]["cidr"] for b in egress[2]["to"]] == ["140.82.112.0/20", "192.30.252.0/22"]  # IPv4, deduped
    assert egress[1]["ports"] == [{"port": 8080, "protocol": "TCP"}]
    assert all(peer.get("namespaceSelector") for rule in egress[:2] for peer in rule["to"])  # never a bare podSelector
    assert out["items"][1]["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == HOST
    assert len(egress) == 3 and len(out["items"]) == 2  # the apply route is the IaC module's
    try:
        render({})
        raise AssertionError("empty meta accepted")
    except ValueError:
        pass
    signer = render({"api": ["140.82.112.0/20"]}, ["10.81.0.37", "10.81.0.12", "10.81.0.37"])["items"][0]["spec"]["egress"]
    assert signer[3] == {"to": [{"ipBlock": {"cidr": "10.81.0.12/32"}}, {"ipBlock": {"cidr": "10.81.0.37/32"}}],
                         "ports": [{"port": 443, "protocol": "TCP"}]}  # deduped /32s, nothing wider
    for bad in ([], ["52.95.1.1"]):
        try:
            render({"api": ["140.82.112.0/20"]}, bad)
            raise AssertionError("public or empty endpoint accepted")
        except ValueError:
            pass


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _check()
        print("ok")
    else:
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--github-meta")
        parser.add_argument("--kube-api", action="append", help="API server endpoint ip:port (alone: only that policy)")
        parser.add_argument("--artifact-storage", action="append", help="artifact storage public IPv4 (alone: only that policy)")
        parser.add_argument("--aws-endpoints", help="JSON list: platform-gate-signer output endpoint_ips")
        args = parser.parse_args()
        if args.artifact_storage:
            json.dump(artifact_storage(args.artifact_storage), sys.stdout, indent=1)
            sys.exit(0)
        if args.kube_api:
            json.dump(kube_api(args.kube_api), sys.stdout, indent=1)
            sys.exit(0)
        if not args.github_meta:
            parser.error("--github-meta is required")
        endpoints = None
        if args.aws_endpoints:
            with open(args.aws_endpoints) as stream:
                endpoints = json.load(stream)
        with open(args.github_meta) as stream:
            json.dump(render(json.load(stream), endpoints), sys.stdout, indent=1)
