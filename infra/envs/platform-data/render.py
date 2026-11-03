"""Render non-secret platform DB resources; credentials are created on the host."""
import argparse
import hashlib
import json
import re
from pathlib import Path

NS = "platform-state"
DB = "platform-db"
IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
LABELS = {"app.kubernetes.io/part-of": "bravo-platform", "component": "platform"}


def obj(api, kind, name, *, namespace=NS, spec=None, **fields):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, "labels": LABELS.copy()}}
    if namespace:
        result["metadata"]["namespace"] = namespace
    if spec is not None:
        result["spec"] = spec
    return result | fields


def namespace(name):
    result = obj("v1", "Namespace", name, namespace=None)
    result["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted",
                                        "pod-security.kubernetes.io/enforce-version": "v1.35"})
    return result


def render(node, bootstrap_sql, migration_sql):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", node):
        raise ValueError("invalid_node_name")
    # These exact T10 migration files are supplied from a separately reviewed checkout.
    sql = (bootstrap_sql + "\nCREATE ROLE platform_migration LOGIN IN ROLE bravo_platform_migrator;\n"
           "CREATE ROLE platform_runtime LOGIN IN ROLE bravo_platform_runtime;\n" + migration_sql +
           "\nREVOKE ALL ON DATABASE platform FROM PUBLIC;\n"
           "GRANT CONNECT ON DATABASE platform TO platform_migration, platform_runtime;\n"
           "ALTER ROLE platform_runtime SET search_path TO pg_catalog, bravo_platform;\n")
    config = obj("v1", "ConfigMap", "platform-db-bootstrap", data={"schema.sql": sql})
    config["metadata"]["annotations"] = {"bravo.dev/t10-bootstrap-sha256": hashlib.sha256(bootstrap_sql.encode()).hexdigest(),
                                          "bravo.dev/t10-migration-sha256": hashlib.sha256(migration_sql.encode()).hexdigest()}
    resources = [namespace(NS),
        obj("v1", "ServiceAccount", "platform-runtime", automountServiceAccountToken=False),
        obj("storage.k8s.io/v1", "StorageClass", "platform-local", namespace=None,
            provisioner="kubernetes.io/no-provisioner", volumeBindingMode="WaitForFirstConsumer", reclaimPolicy="Retain"),
        obj("v1", "PersistentVolume", "platform-state-data", namespace=None, spec={
            "capacity": {"storage": "16Gi"}, "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"],
            "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "platform-local",
            "local": {"path": "/var/lib/bravo-platform-data/postgres"},
            "claimRef": {"namespace": NS, "name": "platform-db-1"},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}]}}}), config]
    resources += [obj("postgresql.cnpg.io/v1", "Cluster", DB, spec={
        "instances": 1, "imageName": IMAGE, "enableSuperuserAccess": False,
        "storage": {"size": "16Gi", "storageClass": "platform-local"},
        "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        "postgresql": {"parameters": {"password_encryption": "scram-sha-256", "log_connections": "on",
            "log_disconnections": "on", "log_statement": "none"},
            "pg_hba": ["hostnossl all all all reject", "hostssl platform platform_runtime all scram-sha-256",
                       "hostssl platform platform_migration all scram-sha-256", "host all all all reject"]},
        "bootstrap": {"initdb": {"database": "platform", "owner": "platform_bootstrap",
            "postInitApplicationSQLRefs": {"configMapRefs": [{"name": "platform-db-bootstrap", "key": "schema.sql"}]}}},
        "managed": {"roles": [{"name": name, "ensure": "present", "login": True, "superuser": False,
            "createdb": False, "createrole": False, "replication": False, "bypassrls": False,
            "inRoles": [role], "passwordSecret": {"name": secret}}
            for name, role, secret in [("platform_runtime", "bravo_platform_runtime", "platform-runtime-db"),
                                       ("platform_migration", "bravo_platform_migrator", "platform-migration-db")]]}})]
    resources += [obj("networking.k8s.io/v1", "NetworkPolicy", "default-deny", spec={
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        obj("networking.k8s.io/v1", "NetworkPolicy", "runtime-database", spec={
            "podSelector": {"matchLabels": {"bravo.dev/db-client": "runtime"}}, "policyTypes": ["Egress"],
            "egress": [{"to": [{"podSelector": {"matchLabels": {"cnpg.io/cluster": DB}}}],
                        "ports": [{"protocol": "TCP", "port": 5432}]},
                       {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}),
        obj("networking.k8s.io/v1", "NetworkPolicy", "database", spec={
            "podSelector": {"matchLabels": {"cnpg.io/cluster": DB}}, "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {"from": [{"podSelector": {"matchLabels": {"bravo.dev/db-client": "runtime"}}}],
                 "ports": [{"protocol": "TCP", "port": 5432}]},
                {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"}}}],
                 "ports": [{"protocol": "TCP", "port": 8000}]}],
            # CNPG instance manager communicates with the Kubernetes API. Node/Pod egress
            # policy is distinct from the host's AWS role (which cannot read Secrets).
            "egress": [{"ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}]},
                       {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]})]
    # No cross-namespace Secret sharing or automatic workload token mounts.
    for name in ("platform-collector", "platform-executor"):
        resources += [namespace(name), obj("v1", "ServiceAccount", name, namespace=name, automountServiceAccountToken=False),
                      obj("networking.k8s.io/v1", "NetworkPolicy", "default-deny", namespace=name,
                          spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})]
    return {"apiVersion": "v1", "kind": "List", "items": resources}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--node", required=True)
    p.add_argument("--consumer-root", type=Path, required=True)
    args = p.parse_args()
    sql_dir = args.consumer_root / "services/platform-events/sql"
    print(json.dumps(render(args.node, (sql_dir / "bootstrap_roles.sql").read_text(),
                            (sql_dir / "0001_state_event.sql").read_text()), indent=2))
