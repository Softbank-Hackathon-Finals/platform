"""Render per-environment collector/Executor identities, never generic exec access."""
import argparse
import json
import re


def render(namespace, pooler_config):
    for value in (namespace, pooler_config):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", value):
            raise ValueError("invalid_resource_name")
    items = []
    for identity in ("collector", "executor"):
        name = "bravo-" + identity
        meta = {"name": name, "namespace": namespace,
                "labels": {"app.kubernetes.io/part-of": "bravo-platform-access", "component": "platform"}}
        items.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta,
                      "automountServiceAccountToken": False})
        rules = [{"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["get", "list", "watch"]},
                 {"apiGroups": ["postgresql.cnpg.io"], "resources": ["clusters"], "verbs": ["get", "list", "watch"]}]
        # PgBouncer ini can contain user/auth configuration: do not expose to collector.
        if identity == "executor":
            rules.append({"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [pooler_config], "verbs": ["get", "patch"]})
        items += [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta, "rules": rules},
                  {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta,
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                   "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]}]
    return {"apiVersion": "v1", "kind": "List", "items": items}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pooler-config", required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.namespace, args.pooler_config), indent=2))
