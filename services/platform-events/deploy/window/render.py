"""T10-only receiver resources; existing T7 DB roles/policies and Operator are prerequisites."""

import argparse
import json
import re

NS = "platform-state"
NAME = "t10-window-intake"
LABELS = {"app.kubernetes.io/name": NAME, "component": "platform"}
PROXY = {"tailscale.com/managed": "true", "tailscale.com/parent-resource": NAME,
         "tailscale.com/parent-resource-ns": NS, "tailscale.com/parent-resource-type": "svc"}


def obj(api, kind, name, **kwargs):
    return {"apiVersion": api, "kind": kind,
            "metadata": {"name": name, "namespace": NS, "labels": LABELS}, **kwargs}


def render(image):
    # The import operator verifies the immutable archive first. No registry login
    # or mutable remote tag is used; a missing node cache fails closed.
    if not re.fullmatch(r"bravo-window-intake:source-[a-f0-9]{64}", image):
        raise ValueError("source_bound_image_required")
    mounts = [{"name": name, "mountPath": path, "readOnly": True} for name, path in
              (("credential", "/credential"), ("ca", "/ca"), ("producer", "/producer"))]
    deployment = obj("apps/v1", "Deployment", NAME, spec={
        "replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": LABELS}, "template": {
            "metadata": {"labels": LABELS | {"bravo.dev/db-client": "runtime"}}, "spec": {
                "serviceAccountName": NAME, "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "receiver", "image": image, "imagePullPolicy": "Never",
                    "ports": [{"containerPort": 8080}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "50m", "memory": "96Mi"},
                                  "limits": {"cpu": "500m", "memory": "256Mi"}},
                    "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}, "periodSeconds": 10},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "initialDelaySeconds": 15},
                    "volumeMounts": mounts}],
                "volumes": [{"name": name, "secret": {"secretName": secret, "defaultMode": 288}} for name, secret in
                            (("credential", "platform-runtime-db"), ("ca", "platform-db-ca"),
                             ("producer", "t10-window-producer-identity"))]}}})
    service = obj("v1", "Service", NAME, spec={"type": "ClusterIP", "selector": LABELS,
                  "ports": [{"name": "window", "port": 30981, "targetPort": 8080, "protocol": "TCP"}]})
    service["metadata"]["annotations"] = {"tailscale.com/expose": "true",
        "tailscale.com/hostname": "bravo-window-intake", "tailscale.com/tags": "tag:bravo-window-intake"}
    policy = obj("networking.k8s.io/v1", "NetworkPolicy", NAME, spec={
        "podSelector": {"matchLabels": LABELS}, "policyTypes": ["Ingress"], "ingress": [{
            "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tailscale-system"}},
                      "podSelector": {"matchLabels": PROXY}}], "ports": [{"protocol": "TCP", "port": 8080}]}]})
    return {"apiVersion": "v1", "kind": "List", "items": [
        obj("v1", "ServiceAccount", NAME, automountServiceAccountToken=False), deployment, service, policy]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    print(json.dumps(render(parser.parse_args().image)))
