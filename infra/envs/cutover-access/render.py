"""Render an explicit Executor capability grant for the current app/pooler pods.

No API calls or credentials. Apply separately in each workload cluster after
checking the pod owners; pod replacement requires operator re-authorization.
"""

import argparse
import json
import re


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise ValueError("invalid_resource_name")
    return value


def render(namespace, pooler_config, app_pods, pooler_pod, identity_namespace=None,
           pooler_only=False):
    identity_namespace = identity_namespace or namespace
    for value in [namespace, identity_namespace, pooler_config, pooler_pod, *app_pods]:
        name(value)
    if namespace in {"default", "kube-system", "kube-public", "kube-node-lease"}:
        raise ValueError("dedicated_workload_namespace_required")
    if (pooler_only and app_pods) or (not pooler_only and not app_pods) or pooler_pod in app_pods:
        raise ValueError("distinct_app_and_pooler_pods_required")
    meta = {"name": "bravo-executor-cutover", "namespace": namespace,
            "labels": {"app.kubernetes.io/part-of": "bravo-platform-access", "component": "platform"}}
    rules = [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        # Named connect requests carry the pod name even when the verb is create.
        # No namespace-wide exec: CNPG shares source-dev with app and pooler.
        {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get", "create"],
         "resourceNames": sorted({*app_pods, pooler_pod})},
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "patch"],
         "resourceNames": [pooler_config]},
    ]
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta, "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta,
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": meta["name"]},
         "subjects": [{"kind": "ServiceAccount", "name": "bravo-executor", "namespace": identity_namespace}]},
    ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--identity-namespace")
    parser.add_argument("--pooler-config", required=True)
    parser.add_argument("--pooler-pod", required=True)
    workloads = parser.add_mutually_exclusive_group(required=True)
    workloads.add_argument("--app-pod", action="append")
    workloads.add_argument("--pooler-only", action="store_true",
                           help="Grant only the current pooler; no app capability or cutover readiness")
    args = parser.parse_args()
    try:
        result = render(args.namespace, args.pooler_config, args.app_pod or [], args.pooler_pod,
                        args.identity_namespace, args.pooler_only)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
