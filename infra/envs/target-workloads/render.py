#!/usr/bin/env python3
"""Render a new, isolated T30 development database; never contact a cluster.

The operator must confirm the target identity and empty namespace first. This
manifest does not create a subscription, open application writes or configure
network access to the source environment.
"""
import argparse
import json
import re
from pathlib import Path

NAMESPACE = "target-dev"
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
LABELS = {"app.kubernetes.io/part-of": "bravo-target", "component": "target"}
SUBSCRIPTION_METRICS = "target-subscription-metrics"
# CNPG's default is 5 s. On the relayed tailnet path a few seconds of congestion ended the forward connection on
# both ends at once (2026-11-06). Each cluster sends one direction and receives the other, so both carry both
# values; source-workloads/render.py must say the same. Both settings take effect on reload, without a restart.
REPLICATION_TIMEOUTS = {"wal_sender_timeout": "30s", "wal_receiver_timeout": "30s"}
# CNPG's exporter turns each column into cnpg_pg_subscription_<column> and caches results for 30 seconds. It runs
# as cnpg_metrics_exporter, which may read every pg_subscription column named here but not subconninfo. A worker
# that is still connecting already counts in apply_workers, so 1 does not prove rows are flowing; the source
# slot's lag answers that.
SUBSCRIPTION_QUERIES = {"pg_subscription": {
    "primary": True,
    "query": ("SELECT s.subname, s.subenabled::int AS enabled, e.apply_error_count, "
              "(SELECT count(*) FROM pg_catalog.pg_stat_subscription AS w "
              "WHERE w.subid = s.oid AND w.worker_type = 'apply') AS apply_workers "
              "FROM pg_catalog.pg_subscription AS s "
              "JOIN pg_catalog.pg_stat_subscription_stats AS e ON e.subid = s.oid"),
    "metrics": [
        {"subname": {"usage": "LABEL", "description": "Subscription name"}},
        {"enabled": {"usage": "GAUGE", "description": "1 if the subscription is enabled"}},
        {"apply_error_count": {"usage": "COUNTER", "description": "Errors the apply worker hit, connection failures included"}},
        {"apply_workers": {"usage": "GAUGE", "description": "Apply workers that exist, connected or not"}},
    ],
}}


# One account per T30 workload, each without a role or a token: none of these Pods calls the Kubernetes API.
# kube-bench (CIS 5.1.5, 5.1.6) counts a Pod on the namespace's default account, or on an account that leaves
# the token unset, as a finding even when the Pod turns the token off itself.
ACCOUNTS = ("demo-chat", "target-pgbouncer", "target-seaweedfs", "target-chat-schema", "target-probe",
            "attachment-copy")


def resource(api, kind, name, *, spec=None, namespaced=True, **fields):
    metadata = {"name": name, "labels": LABELS.copy()}
    if namespaced:
        metadata["namespace"] = NAMESPACE
    result = {"apiVersion": api, "kind": kind, "metadata": metadata, **fields}
    if spec is not None:
        result["spec"] = spec
    return result


def subscription_metrics():
    # JSON is a subset of YAML, the format CNPG reads this key as. The label makes instances pick up changes.
    config = resource("v1", "ConfigMap", SUBSCRIPTION_METRICS, data={"queries": json.dumps(SUBSCRIPTION_QUERIES, indent=2)})
    config["metadata"]["labels"]["cnpg.io/reload"] = "true"
    return config


def database(node_name):
    if not isinstance(node_name, str) or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", node_name
    ):
        raise ValueError("exact target Kubernetes node name required")
    namespace = resource("v1", "Namespace", NAMESPACE, namespaced=False)
    namespace["metadata"]["labels"].update({
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
    })
    storage = resource(
        "storage.k8s.io/v1", "StorageClass", "bravo-target-local", namespaced=False,
        provisioner="kubernetes.io/no-provisioner", reclaimPolicy="Retain",
        volumeBindingMode="WaitForFirstConsumer",
    )
    volume = resource("v1", "PersistentVolume", "bravo-target-dev-data", namespaced=False, spec={
        "capacity": {"storage": "8Gi"}, "volumeMode": "Filesystem",
        "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "bravo-target-local",
        "local": {"path": "/var/lib/bravo-target-dev/postgres"},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{
            "key": "kubernetes.io/hostname", "operator": "In", "values": [node_name],
        }]}]}},
    })
    cluster = resource("postgresql.cnpg.io/v1", "Cluster", "target-db", spec={
        "instances": 1, "imageName": POSTGRES_IMAGE, "enableSuperuserAccess": False,
        "storage": {"size": "8Gi", "storageClass": "bravo-target-local"},
        "certificates": {"serverAltDNSNames": [
            "target-pgbouncer", "target-pgbouncer.target-dev",
            "target-pgbouncer.target-dev.svc", "target-pgbouncer.target-dev.svc.cluster.local",
        ]},
        "resources": {"requests": {"cpu": "200m", "memory": "512Mi"},
                      "limits": {"cpu": "1", "memory": "1Gi"}},
        "postgresql": {
            "parameters": {"wal_level": "logical", "password_encryption": "scram-sha-256",
                           "log_connections": "on", "log_disconnections": "on",
                           "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "off",
                           "log_statement": "none", **REPLICATION_TIMEOUTS},
            "pg_hba": ["hostnossl all all all reject",
                       "hostssl chat chat_owner all scram-sha-256",
                       "hostssl chat chat_app all scram-sha-256",
                       "hostssl chat chat_replicator all scram-sha-256",
                       "host all all all reject"],
        },
        "monitoring": {"customQueriesConfigMap": [{"name": SUBSCRIPTION_METRICS, "key": "queries"}]},
        "bootstrap": {"initdb": {"database": "chat", "owner": "chat_owner"}},
        "managed": {"roles": [
            {"name": "chat_app", "ensure": "present", "login": False,
             "superuser": False, "createdb": False, "createrole": False,
             "passwordSecret": {"name": "target-app-db"}},
            {"name": "chat_replicator", "ensure": "present", "login": True,
             "replication": True, "superuser": False, "createdb": False, "createrole": False,
             "passwordSecret": {"name": "target-replication"}},
        ]},
    })
    return [namespace, storage, volume, subscription_metrics(), cluster]


def accounts():
    """Apply after the namespace and before any workload: a Pod whose account is missing is never created."""
    return [resource("v1", "ServiceAccount", name, automountServiceAccountToken=False)
            for name in ("default", *ACCOUNTS)]


def migration():
    """Explicit new-database migration, separate from the T30 empty bootstrap."""
    config = resource("v1", "ConfigMap", "target-chat-schema-v1", data={
        "schema.sql": Path(__file__).with_name("schema.sql").read_text(),
    })
    job = resource("batch/v1", "Job", "target-chat-schema-v1", spec={
        "backoffLimit": 0, "activeDeadlineSeconds": 300,
        "template": {"metadata": {"labels": LABELS.copy()}, "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "serviceAccountName": "target-chat-schema",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 26, "runAsGroup": 26,
                                "fsGroup": 26, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "migration", "image": POSTGRES_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": ["psql", "-X", "--set=ON_ERROR_STOP=1", "--single-transaction",
                            "--file=/migration/schema.sql"],
                "env": [{"name": "PGHOST", "value": "target-db-rw.target-dev.svc"},
                        {"name": "PGDATABASE", "value": "chat"},
                        {"name": "PGUSER", "value": "chat_owner"},
                        {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                            "name": "target-db-app", "key": "password"}}},
                        {"name": "PGSSLMODE", "value": "verify-full"},
                        {"name": "PGSSLROOTCERT", "value": "/tls/ca.crt"},
                        {"name": "PGCONNECT_TIMEOUT", "value": "10"}],
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"cpu": "250m", "memory": "256Mi"}},
                "volumeMounts": [{"name": "schema", "mountPath": "/migration", "readOnly": True},
                                 {"name": "ca", "mountPath": "/tls", "readOnly": True}],
            }],
            "volumes": [{"name": "schema", "configMap": {"name": "target-chat-schema-v1"}},
                        {"name": "ca", "secret": {"secretName": "target-db-ca",
                            "items": [{"key": "ca.crt", "path": "ca.crt"}]}}],
        }},
    })
    return [config, job]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["database", "accounts", "migration"])
    parser.add_argument("--node-name")
    args = parser.parse_args()
    objects = {"database": lambda: database(args.node_name), "accounts": accounts, "migration": migration}[args.stage]()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))


if __name__ == "__main__":
    main()
