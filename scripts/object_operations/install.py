"""Install one bounded, periodically recovered executor; no credentials in these manifests."""
import hashlib
import json
from pathlib import Path

NAMESPACE = "target-dev"
NAME = "bravo-object-operations"
IMAGE = "docker.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
ROOT = Path(__file__).resolve().parents[2]


def manifests(cluster_uid, secret_uid, deployment_uid):
    code = {"worker.py": (Path(__file__).with_name("worker.py")).read_text(),
            "server.py": (Path(__file__).with_name("server.py")).read_text(),
            "openapi.json": (ROOT / "docs/object-operations.openapi.json").read_text(),
            "object_exposure.py": (ROOT / "infra/envs/target-workloads/object_exposure.py").read_text()}
    digest = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
    code_name = NAME + "-" + digest[:12]
    meta = lambda name: {"name": name, "namespace": NAMESPACE, "labels": {"app.kubernetes.io/managed-by": NAME}}
    state = {"schema": "bravo.object-operation-state/v1", "phase": "IDLE", "history": [],
             "config": {"cluster_uid": cluster_uid, "secret_uid": secret_uid,
                        "deployment_uid": deployment_uid, "source_sha256": digest}}
    objects = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(code_name), "immutable": True, "data": code},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(NAME)},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta(NAME), "rules": [
            {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [NAME], "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["target-seaweedfs-auth"], "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["target-app-objects"], "verbs": ["get"]},
            {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["target-seaweedfs"], "verbs": ["get", "patch"]},
        ]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(NAME),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": NAME},
         "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": NAME},
         "rules": [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": NAME},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NAME},
         "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}]},
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": meta(NAME), "spec": {
            "schedule": "* * * * *", "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 20,
            "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 1,
            "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300, "ttlSecondsAfterFinished": 600,
                "template": {"metadata": {"labels": {"app.kubernetes.io/name": NAME, "bravo-object-client": "true"}},
                    "spec": {"serviceAccountName": NAME, "restartPolicy": "Never", "terminationGracePeriodSeconds": 10,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{"name": "operation", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-B", "/code/worker.py"],
                            "env": [{"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}],
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}},
                            "volumeMounts": [{"name": "code", "mountPath": "/code", "readOnly": True}]}],
                        "volumes": [{"name": "code", "configMap": {"name": code_name, "defaultMode": 292}}],
                    }}}}},
        },
    ]
    api_name = NAME + "-api"
    objects += [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(api_name)},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta(api_name),
         "rules": [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [NAME], "verbs": ["get", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(api_name),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": api_name},
         "subjects": [{"kind": "ServiceAccount", "name": api_name, "namespace": NAMESPACE}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": api_name},
         "rules": [{"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]},
                   {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": api_name},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": api_name},
         "subjects": [{"kind": "ServiceAccount", "name": api_name, "namespace": NAMESPACE}]},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(NAME),
         "spec": {"selector": {"app.kubernetes.io/name": api_name}, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(api_name), "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app.kubernetes.io/name": api_name}},
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": api_name}}, "spec": {
                "serviceAccountName": api_name,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "api", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-B", "/code/server.py"], "ports": [{"name": "http", "containerPort": 8080}],
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 2},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}},
                    "volumeMounts": [{"name": "code", "mountPath": "/code", "readOnly": True}]}],
                "volumes": [{"name": "code", "configMap": {"name": code_name, "defaultMode": 292}}],
            }}}},
    ]
    initial = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(NAME), "data": {"state.json": json.dumps(state)}}
    return objects, initial
