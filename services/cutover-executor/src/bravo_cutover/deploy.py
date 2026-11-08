"""Render credential-free platform manifests; Secrets are supplied by the operator."""

import argparse
import json
import re

NAME = "cutover-executor"
NAMESPACE = "bravo-platform"


def manifests(image: str, *, migration: bool = False, pair: str = "") -> dict:
    """`pair` names one more Executor for another fixed DB pair (ADR-0011: one Executor per pair).

    Empty renders the existing names unchanged. Every object and Secret name, and the Pod label the
    network policies select, take the suffix, so the instances never share a Service, Secret or route.
    """
    if pair and not re.fullmatch(r"[a-z][a-z0-9]{0,15}", pair):
        raise ValueError("pair must be a short lowercase name")
    name = f"{NAME}-{pair}" if pair else NAME
    labels = {"app.kubernetes.io/name": name}
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("an immutable image digest is required")
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}}
    container = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent",
                 "securityContext": security,
                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                               "limits": {"cpu": "1", "memory": "512Mi"}},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"},
                                  {"name": "state-ca", "mountPath": "/run/state-ca", "readOnly": True}]}
    pod = {"serviceAccountName": name, "automountServiceAccountToken": False,
           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                               "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [container], "terminationGracePeriodSeconds": 60,
           "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
                       {"name": "state-ca", "secret": {"secretName": name + "-state-ca",
                                                     "defaultMode": 0o440}}]}
    sa = {"apiVersion": "v1", "kind": "ServiceAccount",
          "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
          "automountServiceAccountToken": False}
    if migration:
        container["command"] = ["python", "-m", "bravo_cutover.migrate"]
        container["env"] = [{"name": "EXECUTOR_MIGRATION_DB", "valueFrom": {
            "secretKeyRef": {"name": name + "-migration", "key": "dsn"}}}]
        pod["restartPolicy"] = "Never"
        workload = {"apiVersion": "batch/v1", "kind": "Job",
                    "metadata": {"name": name + "-migration", "namespace": NAMESPACE},
                    "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 120,
                             "template": {"metadata": {"labels": labels}, "spec": pod}}}
        return {"apiVersion": "v1", "kind": "List", "items": [sa, workload]}
    container["env"] = [
        {"name": "EXECUTOR_DB", "valueFrom": {
            "secretKeyRef": {"name": name + "-state", "key": "dsn"}}},
        {"name": "EXECUTOR_CONFIG", "value": "/run/executor/sites.json"},
        {"name": "EXECUTOR_API_TOKEN_FILE", "value": "/run/executor/api-token"},
        {"name": "EXECUTOR_COMMAND_TOKEN_FILE", "value": "/run/command/token"},
        {"name": "KUBECONFIG", "value": "/run/executor/kubeconfig"},
    ]
    pod["volumes"].append({"name": "credentials", "secret": {
        "secretName": name + "-runtime", "defaultMode": 0o440}})
    container["volumeMounts"].append({"name": "credentials", "mountPath": "/run/executor", "readOnly": True})
    pod["volumes"].append({"name": "command", "secret": {
        "secretName": name + "-command", "items": [{"key": "token", "path": "token"}],
        "defaultMode": 0o440}})
    container["volumeMounts"].append({"name": "command", "mountPath": "/run/command", "readOnly": True})
    container["ports"] = [{"name": "http", "containerPort": 8080}]
    container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": "http", "scheme": "HTTPS"},
                                   "initialDelaySeconds": 3, "periodSeconds": 5}
    # No liveness kill during a cutover: process loss must enter recovery/SAFE_HOLD.
    workload = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                         "selector": {"matchLabels": labels},
                         "template": {"metadata": {"labels": labels}, "spec": pod}}}
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": name, "namespace": NAMESPACE},
               "spec": {"type": "ClusterIP", "selector": labels,
                        "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [sa, workload, service]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--migration", action="store_true")
    parser.add_argument("--pair", default="")
    args = parser.parse_args()
    print(json.dumps(manifests(args.image, migration=args.migration, pair=args.pair)))


if __name__ == "__main__":
    main()
