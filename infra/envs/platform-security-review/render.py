"""Render the retained audit volume required by the platform Security Review service."""

import argparse
import json
import re


NAME = "security-review-audit"
NAMESPACE = "platform-state"
PATH = "/var/lib/bravo-security-review-audit"
LABELS = {"app.kubernetes.io/name": "security-review", "component": "platform"}


def render(node: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node):
        raise ValueError("invalid_node")
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": NAME, "labels": LABELS},
        "spec": {
            "capacity": {"storage": "1Gi"},
            "volumeMode": "Filesystem",
            "accessModes": ["ReadWriteOnce"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "platform-local",
            "local": {"path": PATH},
            "claimRef": {"namespace": NAMESPACE, "name": NAME},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [node],
                                }
                            ]
                        }
                    ]
                }
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    print(json.dumps(render(parser.parse_args().node), indent=2))


if __name__ == "__main__":
    main()
