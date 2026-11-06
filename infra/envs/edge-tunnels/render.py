"""Render a connector for an existing, remotely managed Cloudflare tunnel.

The tunnel token must already be in the namespace's Kubernetes Secret. This
renderer never accepts or emits the token itself.
"""
import argparse
import json

IMAGE = "cloudflare/cloudflared:2026.10.5@sha256:1b1d34d0a9c0bfca9de72db8460d328db3f19edd2b96169114e3e893a97bbe53"
NAME = "bravo-edge-tunnel"


def render(namespace: str) -> dict:
    if namespace not in {"source-dev", "target-dev"}:
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
    parser.add_argument("namespace", choices=["source-dev", "target-dev"])
    print(json.dumps(render(parser.parse_args().namespace), indent=2))
