"""Render the platform-side prerequisites of the T33 findings API; the cluster builder applies them on the host.

    python3 infra/envs/platform-security/render.py --node <platform node name> | kubectl apply -f -

Namespace (restricted), the static local PV bound to the API's claim, and a namespace default-deny. The workload
itself is GitOps (services/security-findings/deploy/platform-gitops); Secrets are created outside Git.
"""
import argparse
import json
import re

NS = "bravo-security"
CLAIM = "bravo-findings-data"
PATH = "/var/lib/bravo-platform-data/security-findings"
LABELS = {"app.kubernetes.io/part-of": "bravo-platform", "component": "platform"}


def obj(api, kind, name, *, namespace=NS, spec=None, **fields):
    result = {"apiVersion": api, "kind": kind, "metadata": {"name": name, "labels": LABELS.copy()}}
    if namespace:
        result["metadata"]["namespace"] = namespace
    if spec is not None:
        result["spec"] = spec
    return result | fields


def render(node):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node):
        raise ValueError("invalid_node_name")
    namespace = obj("v1", "Namespace", NS, namespace=None)
    namespace["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted",
                                            "pod-security.kubernetes.io/enforce-version": "v1.35"})
    volume = obj("v1", "PersistentVolume", "platform-security-findings-data", namespace=None, spec={
        "capacity": {"storage": "1Gi"}, "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "platform-local",
        "local": {"path": PATH},
        "claimRef": {"namespace": NS, "name": CLAIM},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}]}}})
    deny = obj("networking.k8s.io/v1", "NetworkPolicy", "default-deny", spec={"podSelector": {},
                                                                              "policyTypes": ["Ingress", "Egress"]})
    return [namespace, volume, deny]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node", required=True)
    args = parser.parse_args(argv)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": render(args.node)}, indent=2))


if __name__ == "__main__":
    main()
