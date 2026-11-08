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


COLLECTOR = "bravo-t14-collector"
COLLECTOR_NAMESPACE = "bravo-apply-access"  # where the platform's other target identities live


COLLECTOR_USER = f"system:serviceaccount:{COLLECTOR_NAMESPACE}:{COLLECTOR}"
# Pods the collector may reach. Deployment Pods by their owner prefix; never target-db-* (#765 review).
FORWARD_PODS = {"target-dev": ("target-seaweedfs-", "target-pgbouncer-"), "bravo-secrets": ("target-openbao-0",)}


def collector_admission():
    """ADR-0041: what RBAC cannot bound, admission does, for this identity only (installed before the grants).

    TokenRequest: exactly audience openbao and 600 s. RBAC cannot limit a TokenRequest's audience or
    lifetime and the API server sets no maximum, so without this the kubeconfig could mint a long-lived
    demo-chat token (#666 review 2). Port-forward: only the three service Pods, not the database Pod.
    """
    actor = f"request.userInfo.username == {json.dumps(COLLECTOR_USER)}"
    allowed = " || ".join(
        f"(request.namespace == {json.dumps(ns)} && ({' || '.join(f'request.name.startsWith({json.dumps(p)})' if p.endswith('-') else f'request.name == {json.dumps(p)}' for p in pods)}))"
        for ns, pods in FORWARD_PODS.items())

    def policy(name, operation, resources, expression, message):
        return [{"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
                 "metadata": {"name": name, "labels": LABELS.copy()},
                 "spec": {"failurePolicy": "Fail",
                          "matchConstraints": {"resourceRules": [{"apiGroups": [""], "apiVersions": ["v1"],
                              "operations": [operation], "resources": resources, "scope": "Namespaced"}]},
                          "matchConditions": [{"name": "collector", "expression": actor}],
                          "validations": [{"expression": expression, "message": message, "reason": "Forbidden"}]}},
                {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
                 "metadata": {"name": name, "labels": LABELS.copy()},
                 "spec": {"policyName": name, "validationActions": ["Deny"]}}]

    return [*policy(f"{COLLECTOR}-token", "CREATE", ["serviceaccounts/token"],
                    "object.spec.audiences == ['openbao'] && has(object.spec.expirationSeconds) && "
                    "object.spec.expirationSeconds == 600",
                    "bravo-t14-collector may mint only audience openbao, 600 s tokens"),
            *policy(f"{COLLECTOR}-forward", "CONNECT", ["pods/portforward"], allowed,
                    "bravo-t14-collector may port-forward only to SeaweedFS, the pooler and OpenBao")]


def collector():
    """ADR-0041: the platform's own identity for the post-cutover target run, named resources only.

    It mints the two SEC-01.1 tokens from outside the cluster (the platform fixes audience and lifetime;
    nothing in the cluster gets this verb), reads the app's object client keys for IAM-01.1 and reaches
    OpenBao/SeaweedFS through API port-forwards.
    """
    def namespaced(item, namespace):
        item["metadata"]["namespace"] = namespace
        return item

    subject = [{"kind": "ServiceAccount", "name": COLLECTOR, "namespace": COLLECTOR_NAMESPACE}]
    def forward(pods=None):
        # Deployment Pod names are random, so target-dev cannot be narrowed by name; OpenBao's StatefulSet can.
        names = {"resourceNames": pods} if pods else {}
        return [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                {"apiGroups": [""], "resources": ["pods/portforward"], "verbs": ["create"], **names}]
    rules = {
        "target-dev": [{"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"],
                        "resourceNames": ["demo-chat", OTHER]},
                       {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"],
                        "resourceNames": ["target-object-client"]},
                       {"apiGroups": [""], "resources": ["services"], "verbs": ["get"],
                        "resourceNames": ["seaweedfs", "target-pgbouncer"]},
                       *forward()],
        "bravo-secrets": [{"apiGroups": [""], "resources": ["services"], "verbs": ["get"],
                           "resourceNames": ["target-openbao"]}, *forward(["target-openbao-0"])],
    }
    items = [*collector_admission(),
             namespaced(resource("v1", "ServiceAccount", COLLECTOR, automountServiceAccountToken=False),
                        COLLECTOR_NAMESPACE)]
    for namespace, rule in rules.items():
        items.append(namespaced(resource("rbac.authorization.k8s.io/v1", "Role", COLLECTOR, rules=rule), namespace))
        items.append(namespaced(resource("rbac.authorization.k8s.io/v1", "RoleBinding", COLLECTOR, subjects=subject,
                                         roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                                  "name": COLLECTOR}), namespace))
    return items


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["accounts", "canary", "collector"])
    parser.add_argument("--arch", choices=["amd64", "arm64"], default="amd64")
    args = parser.parse_args()
    items = {"accounts": accounts, "collector": collector}.get(args.stage, lambda: canary(args.arch))()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
