"""Render credential-free platform manifests; Secrets are supplied by the operator."""

import argparse
import json
import re

NAME = "cutover-executor"
NAMESPACE = "bravo-platform"
LABELS = {"app.kubernetes.io/name": NAME}


def manifests(image: str, *, migration: bool = False) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("an immutable image digest is required")
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}}
    container = {"name": NAME, "image": image, "imagePullPolicy": "IfNotPresent",
                 "securityContext": security,
                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                               "limits": {"cpu": "1", "memory": "512Mi"}},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}
    pod = {"serviceAccountName": NAME, "automountServiceAccountToken": False,
           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                               "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [container], "terminationGracePeriodSeconds": 60,
           "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]}
    sa = {"apiVersion": "v1", "kind": "ServiceAccount",
          "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": LABELS},
          "automountServiceAccountToken": False}
    if migration:
        container["command"] = ["python", "-m", "bravo_cutover.migrate"]
        container["env"] = [{"name": "EXECUTOR_MIGRATION_DB", "valueFrom": {
            "secretKeyRef": {"name": "cutover-executor-migration", "key": "dsn"}}}]
        pod["restartPolicy"] = "Never"
        workload = {"apiVersion": "batch/v1", "kind": "Job",
                    "metadata": {"name": NAME + "-migration", "namespace": NAMESPACE},
                    "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 120,
                             "template": {"metadata": {"labels": LABELS}, "spec": pod}}}
        return {"apiVersion": "v1", "kind": "List", "items": [sa, workload]}
    container["env"] = [
        {"name": "EXECUTOR_DB", "valueFrom": {
            "secretKeyRef": {"name": "cutover-executor-state", "key": "dsn"}}},
        {"name": "EXECUTOR_CONFIG", "value": "/run/executor/sites.json"},
        {"name": "EXECUTOR_API_TOKEN_FILE", "value": "/run/executor/api-token"},
        {"name": "KUBECONFIG", "value": "/run/executor/kubeconfig"},
    ]
    pod["volumes"].append({"name": "credentials", "secret": {
        "secretName": "cutover-executor-runtime", "defaultMode": 0o440}})
    container["volumeMounts"].append({"name": "credentials", "mountPath": "/run/executor", "readOnly": True})
    container["ports"] = [{"name": "http", "containerPort": 8080}]
    container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": "http", "scheme": "HTTPS"},
                                   "initialDelaySeconds": 3, "periodSeconds": 5}
    # No liveness kill during a cutover: process loss must enter recovery/SAFE_HOLD.
    workload = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": LABELS},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                         "selector": {"matchLabels": LABELS},
                         "template": {"metadata": {"labels": LABELS}, "spec": pod}}}
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": NAME, "namespace": NAMESPACE},
               "spec": {"type": "ClusterIP", "selector": LABELS,
                        "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [sa, workload, service]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--migration", action="store_true")
    args = parser.parse_args()
    print(json.dumps(manifests(args.image, migration=args.migration)))


if __name__ == "__main__":
    main()
