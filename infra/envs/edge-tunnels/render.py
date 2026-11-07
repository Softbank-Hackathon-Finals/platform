"""Render a connector for an existing, remotely managed Cloudflare tunnel.

The tunnel token must already be in the namespace's Kubernetes Secret. This
renderer never accepts or emits the token itself.
"""
import argparse
import json
import re

IMAGE = "cloudflare/cloudflared:2026.10.5@sha256:1b1d34d0a9c0bfca9de72db8460d328db3f19edd2b96169114e3e893a97bbe53"
NAME = "bravo-edge-tunnel"


# P0 application namespaces. Any registered environment (aws, gcp or onprem,
# source or target; ADR-0034) may pass its own namespace instead.
KNOWN_NAMESPACES = ("source-dev", "target-dev")
NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
RESERVED = {"default", "kube-system", "kube-public", "kube-node-lease"}


def render(namespace: str) -> dict:
    if not NAMESPACE.fullmatch(namespace) or namespace in RESERVED:
        raise ValueError("expected a registered application namespace")
    labels = {"app": NAME, "component": "edge", "app.kubernetes.io/part-of": "bravo"}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1, "selector": {"matchLabels": {"app": NAME}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                        "runAsGroup": 65532, "fsGroup": 65532,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "cloudflared", "image": IMAGE,
                        "args": ["tunnel", "--no-autoupdate", "--protocol", "http2",
                                 "--metrics", "0.0.0.0:2000", "run",
                                 "--token-file", "/run/tunnel/token"],
                        "ports": [{"name": "metrics", "containerPort": 2000}],
                        "readinessProbe": {"httpGet": {"path": "/ready", "port": "metrics"},
                                           "periodSeconds": 5, "timeoutSeconds": 3},
                        "livenessProbe": {"httpGet": {"path": "/ready", "port": "metrics"},
                                          "initialDelaySeconds": 30, "periodSeconds": 10,
                                          "timeoutSeconds": 3, "failureThreshold": 6},
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                      "limits": {"cpu": "500m", "memory": "256Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False,
                                            "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "token", "mountPath": "/run/tunnel", "readOnly": True}],
                    }],
                    "volumes": [{"name": "token", "secret": {
                        "secretName": NAME + "-token", "defaultMode": 288}}],
                },
            },
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace", help="application namespace of the registered environment, "
                        "e.g. " + " or ".join(KNOWN_NAMESPACES))
    args = parser.parse_args()
    try:
        manifest = render(args.namespace)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(manifest, indent=2))
