#!/usr/bin/env python3
"""Render what the T14 demo-gate:1 run needs on the target, and nothing more; never contact a cluster.

`accounts`: SEC-01.1 compares the app's service account with another one in the same namespace. This
renders that other account (`t14-other`, no permissions of its own) and a runner account whose only
right is to mint short tokens for exactly those two accounts, for a collector Job that runs in target-dev.

`canary`: IAM-01.1 needs one fixed object in the attachment bucket. A Job writes it with the bootstrap
identity, then checks what the collector will check: the client identity reads it, an unsigned read is
refused. It prints the size and SHA-256 of the object, never a key.
"""
import argparse
import json
from pathlib import Path

from application import pinned_image
from render import LABELS, resource

OTHER = "t14-other"
RUNNER = "t14-runner"
APP_ACCOUNT = "demo-chat"
SCRIPT = "t14_canary.py"


def accounts():
    """Two tokenless accounts and one narrow token-minting grant. No other verb, resource or name."""
    other = resource("v1", "ServiceAccount", OTHER, automountServiceAccountToken=False)
    runner = resource("v1", "ServiceAccount", RUNNER, automountServiceAccountToken=False)
    role = resource("rbac.authorization.k8s.io/v1", "Role", "t14-token-minter", rules=[{
        "apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"],
        "resourceNames": [APP_ACCOUNT, OTHER],
    }])
    binding = resource("rbac.authorization.k8s.io/v1", "RoleBinding", "t14-token-minter",
                       roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "t14-token-minter"},
                       subjects=[{"kind": "ServiceAccount", "name": RUNNER, "namespace": "target-dev"}])
    return [other, runner, role, binding]


def canary(arch):
    image = pinned_image(arch)
    config = resource("v1", "ConfigMap", "target-t14-canary",
                      data={SCRIPT: Path(__file__).with_name(SCRIPT).read_text()})
    env = [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
           {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    for prefix, secret in (("BOOTSTRAP", "target-object-bootstrap"), ("CLIENT", "target-object-client")):
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            env.append({"name": f"{prefix}_{key}", "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})
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
