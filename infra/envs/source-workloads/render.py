#!/usr/bin/env python3
"""Render the dedicated T6 development namespace without credential values.

AWS infrastructure, identities, secret versions and operator installation are
separate prerequisites. Rendering never contacts AWS or Kubernetes.
"""

import argparse
import json
import re
from pathlib import Path

NAMESPACE = "source-dev"
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
POOLER_IMAGE = "ghcr.io/cloudnative-pg/pgbouncer:1.25.1@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8"
LABELS = {"app.kubernetes.io/part-of": "bravo-source", "component": "source"}
# Same values as target-workloads/render.py; see the reason there. CNPG's 5 s default ended the forward
# connection during a few seconds of congestion on the tailnet path.
REPLICATION_TIMEOUTS = {"wal_sender_timeout": "30s", "wal_receiver_timeout": "30s"}


def resource(api, kind, name, spec=None, **fields):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, "namespace": NAMESPACE, "labels": LABELS.copy()}}
    if spec is not None:
        result["spec"] = spec
    result.update(fields)
    return result


def deployment(name, image, container, volumes, service_account=None):
    container = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent",
                 "securityContext": {"allowPrivilegeEscalation": False,
                                     "readOnlyRootFilesystem": True,
                                     "capabilities": {"drop": ["ALL"]}}, **container}
    pod = {"automountServiceAccountToken": False,
           "securityContext": {"runAsNonRoot": True,
                               "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [container], "volumes": volumes}
    if service_account:
        pod["serviceAccountName"] = service_account
    return resource("apps/v1", "Deployment", name, {
        "replicas": 1, "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {**LABELS, "app": name}}, "spec": pod}})


def service(name, port):
    return resource("v1", "Service", name, {
        "type": "ClusterIP", "selector": {"app": name},
        "ports": [{"name": "tcp", "port": port, "targetPort": port}]})


def database():
    namespace = resource("v1", "Namespace", NAMESPACE)
    namespace["metadata"].pop("namespace")
    namespace["metadata"]["labels"].update({
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.35"})
    storage = resource("storage.k8s.io/v1", "StorageClass", "source-gp3",
                       provisioner="ebs.csi.aws.com", reclaimPolicy="Retain",
                       volumeBindingMode="WaitForFirstConsumer", allowVolumeExpansion=True,
                       parameters={"type": "gp3", "encrypted": "true"})
    storage["metadata"].pop("namespace")
    schema = resource("v1", "ConfigMap", "source-db-bootstrap",
                      data={"schema.sql": Path(__file__).with_name("schema.sql").read_text()})
    cluster = resource("postgresql.cnpg.io/v1", "Cluster", "source-db", {
        "instances": 1, "imageName": POSTGRES_IMAGE,
        "enableSuperuserAccess": False,
        "storage": {"size": "8Gi", "storageClass": "source-gp3"},
        "resources": {"requests": {"cpu": "200m", "memory": "512Mi"},
                      "limits": {"cpu": "1", "memory": "1Gi"}},
        "certificates": {"serverAltDNSNames": [
            "source-pgbouncer", f"source-pgbouncer.{NAMESPACE}",
            f"source-pgbouncer.{NAMESPACE}.svc", f"source-pgbouncer.{NAMESPACE}.svc.cluster.local"]},
        "postgresql": {
            "parameters": {"wal_level": "logical", "password_encryption": "scram-sha-256",
                           "log_connections": "on", "log_disconnections": "on",
                           "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "off",
                           "log_statement": "none", **REPLICATION_TIMEOUTS},
            "pg_hba": ["hostnossl all all all reject",
                       "hostssl chat chat_app all scram-sha-256",
                       "hostssl chat chat_replicator all scram-sha-256",
                       "host all all all reject"]},
        "bootstrap": {"initdb": {"database": "chat", "owner": "chat_owner",
            "postInitSQL": ["CREATE ROLE chat_app LOGIN", "CREATE ROLE chat_replicator LOGIN REPLICATION"],
            "postInitApplicationSQLRefs": {"configMapRefs": [{"name": "source-db-bootstrap", "key": "schema.sql"}]}}},
        "managed": {"roles": [
            {"name": "chat_app", "ensure": "present", "login": True,
             "superuser": False, "createdb": False, "createrole": False,
             "passwordSecret": {"name": "source-app-db"}},
            {"name": "chat_replicator", "ensure": "present", "login": True,
             "replication": True, "superuser": False, "createdb": False, "createrole": False,
             "passwordSecret": {"name": "source-replication"}}]}})
    return [namespace, storage, schema, cluster]


def pooler():
    config = resource("v1", "ConfigMap", "source-pgbouncer-config", data={
        "pgbouncer.ini": Path(__file__).with_name("pgbouncer.ini").read_text()})
    deploy = deployment("source-pgbouncer", POOLER_IMAGE, {
        "ports": [{"containerPort": 5432}],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "250m", "memory": "256Mi"}},
        "readinessProbe": {"tcpSocket": {"port": 5432}},
        "livenessProbe": {"tcpSocket": {"port": 5432}, "initialDelaySeconds": 10},
        "volumeMounts": [{"name": "config", "mountPath": "/etc/pgbouncer"}] +
            [{"name": n, "mountPath": p, "readOnly": True} for n, p in [
            ("auth", "/etc/pgbouncer-auth"),
            ("ca", "/etc/cnpg-ca"), ("tls", "/etc/cnpg-tls")]] +
            [{"name": "tmp", "mountPath": "/tmp"}]}, [
        {"name": "config", "emptyDir": {}},
        {"name": "config-seed", "configMap": {"name": "source-pgbouncer-config"}},
        {"name": "auth", "secret": {"secretName": "source-pgbouncer-auth", "defaultMode": 288}},
        {"name": "ca", "secret": {"secretName": "source-db-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        {"name": "tls", "secret": {"secretName": "source-db-server", "defaultMode": 288}},
        {"name": "tmp", "emptyDir": {}}])
    deploy["spec"]["template"]["spec"]["securityContext"].update(
        {"runAsUser": 998, "runAsGroup": 996, "fsGroup": 996})
    # Service admin commands must address one pooler, including during updates.
    deploy["spec"]["strategy"] = {"type": "Recreate"}
    deploy["spec"]["template"]["spec"]["initContainers"] = [{
        "name": "seed-config", "image": POOLER_IMAGE, "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sh", "-ec", "umask 077; cp /config-seed/pgbouncer.ini /etc/pgbouncer/pgbouncer.ini; chmod 600 /etc/pgbouncer/pgbouncer.ini"],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                      "limits": {"cpu": "100m", "memory": "64Mi"}},
        "volumeMounts": [{"name": "config-seed", "mountPath": "/config-seed", "readOnly": True},
                         {"name": "config", "mountPath": "/etc/pgbouncer"}]}]
    return [config, deploy, service("source-pgbouncer", 5432)]


def application(settings):
    required = {"region", "app_role_arn", "eso_role_arn", "db_secret_id",
                "control_secret_id", "s3_bucket", "app_image"}
    if set(settings) not in (required, required | {"ecr_repository_url"}):
        raise ValueError("application settings must contain exactly the documented metadata fields")
    for field in ("app_role_arn", "eso_role_arn"):
        if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[\w+=,.@/-]+", settings[field]):
            raise ValueError("workload identity must be an AWS IAM role")
    account = settings["app_role_arn"].split(":")[4]
    repositories = ["ghcr.io/softbank-hackathon-finals/demo-chat"]
    if "ecr_repository_url" in settings:
        expected = f"{account}.dkr.ecr.{settings['region']}.amazonaws.com/bravo/demo-chat"
        if settings["ecr_repository_url"] != expected:
            raise ValueError("private registry contract must match the workload account, region and repository")
        repositories.append(settings["ecr_repository_url"])
    suffix = r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}"
    if not any(re.fullmatch(re.escape(repo) + suffix, settings["app_image"]) for repo in repositories):
        raise ValueError("demo-chat must use the exact registered repository pinned by digest")
    accounts = []
    for name, field in [("demo-chat", "app_role_arn"), ("source-secret-reader", "eso_role_arn")]:
        sa = resource("v1", "ServiceAccount", name, automountServiceAccountToken=False)
        sa["metadata"]["annotations"] = {"eks.amazonaws.com/role-arn": settings[field]}
        accounts.append(sa)
    store = resource("external-secrets.io/v1", "SecretStore", "source-secrets", {
        "provider": {"aws": {"service": "SecretsManager", "region": settings["region"],
                             "auth": {"jwt": {"serviceAccountRef": {"name": "source-secret-reader"}}}}}})
    external = resource("external-secrets.io/v1", "ExternalSecret", "demo-chat-control", {
        "refreshInterval": "1h", "secretStoreRef": {"name": "source-secrets", "kind": "SecretStore"},
        "target": {"name": "demo-chat-control", "creationPolicy": "Owner"},
        "data": [{"secretKey": "admin-token", "remoteRef": {
            "key": settings["control_secret_id"], "property": "admin_token"}}]})
    env = {"APP_ENV": "aws", "DB_HOST": f"source-pgbouncer.{NAMESPACE}.svc.cluster.local",
           "DB_PORT": "5432", "DB_NAME": "chat", "DB_SECRET_ID": settings["db_secret_id"],
           "S3_BUCKET": settings["s3_bucket"], "AWS_REGION": settings["region"],
           "AWS_DEFAULT_REGION": settings["region"], "PGSSLMODE": "verify-full",
           "PGSSLROOTCERT": "/etc/cnpg-ca/ca.crt", "WRITE_BLOCKED": "true"}
    app = deployment("demo-chat", settings["app_image"], {
        "name": "app",
        "env": [{"name": k, "value": v} for k, v in env.items()] + [
            {"name": "ADMIN_TOKEN", "valueFrom": {"secretKeyRef": {"name": "demo-chat-control", "key": "admin-token"}}}],
        "ports": [{"containerPort": 8080}],
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "initialDelaySeconds": 10},
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                      "limits": {"cpu": "500m", "memory": "512Mi"}},
        "volumeMounts": [{"name": "ca", "mountPath": "/etc/cnpg-ca", "readOnly": True},
                         {"name": "tmp", "mountPath": "/tmp"}]}, [
        {"name": "ca", "secret": {"secretName": "source-db-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        {"name": "tmp", "emptyDir": {}}], "demo-chat")
    app["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 10001
    return accounts + [store, external, app, service("demo-chat", 8080)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["database", "pooler", "application"])
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    if args.stage == "database":
        objects = database()
    elif args.stage == "pooler":
        objects = pooler()
    else:
        if not args.settings:
            parser.error("application requires --settings containing metadata only")
        try:
            objects = application(json.loads(args.settings.read_text()))
        except (ValueError, OSError):
            parser.error("invalid application metadata; values are not echoed")
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))


if __name__ == "__main__":
    main()
