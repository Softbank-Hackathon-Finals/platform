#!/usr/bin/env python3
"""Render what the T14 demo-gate:1 run needs on the target, and nothing more; never contact a cluster.

`accounts`: SEC-01.1 compares the app's service account with another one in the same namespace. This
renders that other account (`t14-other`, no permissions of its own). The collector runs on the operator's
machine (kubectl create token + port-forward, #644), so nothing in the cluster may mint tokens: RBAC
cannot bound a TokenRequest's audience or lifetime, and such a grant would be a second way to the app's
OpenBao role.

`canary`: IAM-01.1 needs one fixed object in the attachment bucket, byte-identical to the source canary
(source-workloads `aws_s3_object.t14`) so the cutover copy can carry it as an approved extra key. A Job
writes it with the client identity and reads it back; the unsigned read is only observed (the demo starts
in `open-demo`). It prints the size and SHA-256 of the object, never a key.
"""
import argparse
import json
from pathlib import Path

from application import pinned_image
from render import LABELS, resource

OTHER = "t14-other"
SCRIPT = "t14_canary.py"


def accounts():
    """One tokenless account with no permissions. No Role: nothing in the cluster mints tokens for the run."""
    return [resource("v1", "ServiceAccount", OTHER, automountServiceAccountToken=False)]


def canary(arch):
    image = pinned_image(arch)
    config = resource("v1", "ConfigMap", "target-t14-canary",
                      data={SCRIPT: Path(__file__).with_name(SCRIPT).read_text()})
    env = [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
           {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    # The client identity already has Write on the bucket (object_exposure.CLIENT_ACTIONS): no Admin key here.
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        env.append({"name": f"CLIENT_{key}", "valueFrom": {"secretKeyRef": {"name": "target-object-client", "key": key}}})
    job = resource("batch/v1", "Job", "target-t14-canary", spec={
        "backoffLimit": 0, "activeDeadlineSeconds": 300, "ttlSecondsAfterFinished": 86400,
        "template": {"metadata": {"labels": {**LABELS, "bravo-object-client": "true", "bravo-probe": "target-t14-canary"}},
                     "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "serviceAccountName": "target-probe",
            "nodeSelector": {"kubernetes.io/arch": arch},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "canary", "image": image,
                # arm64 runs the local build already on the node; amd64 runs the registered publication.
                "imagePullPolicy": "Never" if arch == "arm64" else "IfNotPresent",
                "command": ["/app/.venv/bin/python", f"/probe/{SCRIPT}"],
                "env": env,
                "volumeMounts": [{"name": "script", "mountPath": "/probe", "readOnly": True}],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
            }],
            "volumes": [{"name": "script", "configMap": {"name": "target-t14-canary"}}],
        }},
    })
    return [config, job]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["accounts", "canary"])
    parser.add_argument("--arch", choices=["amd64", "arm64"], default="amd64")
    args = parser.parse_args()
    items = accounts() if args.stage == "accounts" else canary(args.arch)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
