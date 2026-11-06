"""Retained T31 storage on the existing platform node, without DB volume reuse."""

import argparse
import json
import re

NAMESPACE = "bravo-apply-control"
NAME = "t31-application-records"
LABELS = {"app.kubernetes.io/name": NAME, "component": "platform", "bravo.dev/owner": "t31-host"}


def render(node, namespace=NAMESPACE):
    """namespace: the PVC must live where the host Pod runs; the workflow host chart uses platform-state."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node):
        raise ValueError("invalid_node")
    if namespace not in (NAMESPACE, "platform-state"):
        raise ValueError("invalid_namespace")
    pv = dict(apiVersion="v1", kind="PersistentVolume",
        metadata=dict(name=NAME, labels=LABELS), spec={
            "capacity": {"storage": "1Gi"}, "volumeMode": "Filesystem",
            "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "platform-local", "local": {"path": "/var/lib/bravo-t31"},
            "claimRef": {"namespace": namespace, "name": NAME},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}]}}})
    pvc = dict(apiVersion="v1", kind="PersistentVolumeClaim",
        metadata=dict(name=NAME, namespace=namespace, labels=LABELS), spec={
            "accessModes": ["ReadWriteOnce"], "storageClassName": "platform-local",
            "volumeName": NAME, "resources": {"requests": {"storage": "1Gi"}}})
    return dict(apiVersion="v1", kind="List", items=[pv, pvc])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--node", required=True)
    p.add_argument("--namespace", default=NAMESPACE, choices=(NAMESPACE, "platform-state"))
    a = p.parse_args()
    print(json.dumps(render(a.node, a.namespace), indent=2))
