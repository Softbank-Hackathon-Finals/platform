#!/usr/bin/env python3
"""Check T6 source configuration fragments locally; never contacts providers.

This is not the T5/T19 equivalence gate or proof of a deployed environment.
"""
from __future__ import annotations

import argparse
import configparser
import json
import shlex
from pathlib import Path


def at(value, *keys, default=None):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def text(value):
    return value.strip() if isinstance(value, str) else ""


def audit_class_enabled(value, wanted):
    classes = {part.strip().lower() for part in text(value).split(",")}
    return (wanted in classes or "all" in classes) and f"-{wanted}" not in classes and "-all" not in classes


def check(bundle):
    checks = []

    def expect(identifier, condition, requirement):
        checks.append({"id": identifier, "result": "PASS" if condition else "BLOCK", "requirement": requirement})

    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        expect("input", False, "schema_version=1 configuration object")
        return report(checks)

    eks = bundle.get("eks_request", {})
    log_entries = at(eks, "logging", "clusterLogging", default=[])
    enabled, disabled = set(), set()
    if isinstance(log_entries, list):
        for entry in log_entries:
            types = at(entry, "types", default=[])
            if isinstance(types, list) and all(isinstance(t, str) for t in types):
                if at(entry, "enabled") is True:
                    enabled.update(types)
                elif at(entry, "enabled") is False:
                    disabled.update(types)
    expect("eks.audit", "audit" in enabled and "audit" not in disabled,
           "EKS control-plane audit enabled; CloudTrail does not replace it")

    cluster = bundle.get("cnpg_cluster", {})
    name = text(at(cluster, "metadata", "name"))
    namespace = text(at(cluster, "metadata", "namespace"))
    params = at(cluster, "spec", "postgresql", "parameters", default={})
    expect("cnpg.identity", at(cluster, "kind") == "Cluster" and bool(name) and bool(namespace),
           "CNPG Cluster has an explicit name and namespace")
    expect("db.connections", text(at(params, "log_connections")).lower() in {"on", "true", "1"},
           "CNPG log_connections is enabled for T34 login canaries")
    expect("db.ddl", audit_class_enabled(at(params, "pgaudit.log"), "ddl"),
           "pgaudit includes DDL and does not subtract it")
    expect("db.parameter_privacy", text(at(params, "pgaudit.log_parameter")).lower() in {"off", "false", "0"},
           "pgaudit does not log bound parameter values")
    expect("db.logical", at(params, "wal_level") == "logical",
           "logical WAL intent is explicit; running DB still requires SQL readback")

    service = bundle.get("pooler_service", {})
    service_name = text(at(service, "metadata", "name"))
    service_ns = text(at(service, "metadata", "namespace"))
    ports = at(service, "spec", "ports", default=[])
    expect("pooler.service", at(service, "kind") == "Service" and bool(service_name) and
           service_ns == namespace and at(service, "spec", "type", default="ClusterIP") == "ClusterIP" and
           isinstance(ports, list) and any(at(p, "port") == 5432 and at(p, "targetPort") == 5432 for p in ports),
           "PgBouncer uses a private Service in the DB namespace on port 5432")

    config = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        config.read_string(text(bundle.get("pgbouncer_ini")))
        values = shlex.split(config.get("databases", "chat"))
        pairs = [value.split("=", 1) for value in values]
        if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
            raise ValueError("ambiguous database entry")
        backend = dict(pairs)
        expect("pooler.backend", backend.get("host") == f"{name}-rw.{namespace}.svc.cluster.local" and
               backend.get("port") == "5432" and backend.get("dbname") == "chat" and "password" not in backend,
               "chat backend points to source CNPG primary without inline credentials")
        expect("pooler.tls", config.get("pgbouncer", "server_tls_sslmode", fallback="") == "verify-full" and
               bool(config.get("pgbouncer", "server_tls_ca_file", fallback="")),
               "PgBouncer verifies backend CA and DNS identity")
        expect("pooler.auth", config.get("pgbouncer", "auth_type", fallback="") == "scram-sha-256",
               "PgBouncer client authentication uses SCRAM, never trust/any")
        admins = {v.strip() for v in config.get("pgbouncer", "admin_users", fallback="").split(",") if v.strip()}
        expect("pooler.admin", bool(admins) and not admins.intersection({"chat_app", "replication", "postgres"}),
               "PgBouncer control role is distinct from app, replication and postgres roles")
    except (configparser.Error, ValueError):
        expect("pooler.parse", False, "unambiguous PgBouncer INI with a chat database entry")

    deployment = bundle.get("app_deployment", {})
    containers = at(deployment, "spec", "template", "spec", "containers", default=[])
    apps = [c for c in containers if at(c, "name") == "app"] if isinstance(containers, list) else []
    app = apps[0] if len(apps) == 1 else {}
    expect("app.identity", len(apps) == 1 and at(deployment, "kind") == "Deployment" and
           at(deployment, "metadata", "namespace") == namespace,
           "one selected app container in the source namespace")
    env_list = at(app, "env", default=[])
    env_names = [at(e, "name") for e in env_list] if isinstance(env_list, list) else []
    env = {at(e, "name"): e for e in env_list if isinstance(e, dict)} if isinstance(env_list, list) else {}
    expect("app.env", all(isinstance(n, str) and n for n in env_names) and
           len(env_names) == len(set(env_names)) and not at(app, "envFrom"),
           "explicit, unique env entries; resolve envFrom before this check")
    expect("app.pooler", at(env, "DB_HOST", "value") == f"{service_name}.{namespace}.svc.cluster.local" and
           at(env, "DB_PORT", "value") == "5432",
           "demo-chat DB_HOST/DB_PORT target PgBouncer, not CNPG directly")
    expect("app.sdk_dependency", bool(text(at(env, "DB_SECRET_ID", "value"))) and
           not set(env).intersection({"DB_PASSWORD", "DATABASE_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}),
           "preserve Secrets Manager SDK dependency and use workload identity, without static AWS or DB credentials")
    expect("app.probes", at(app, "readinessProbe", "httpGet", "path") == "/readyz" and
           at(app, "livenessProbe", "httpGet", "path") == "/healthz",
           "DB health is not used for readiness/liveness during cutover")

    store = bundle.get("secret_store", {})
    aws = at(store, "spec", "provider", "aws", default={})
    expect("eso.identity", at(store, "kind") == "SecretStore" and at(store, "metadata", "namespace") == namespace and
           at(aws, "service") == "SecretsManager" and bool(text(at(aws, "region"))) and
           bool(text(at(aws, "auth", "jwt", "serviceAccountRef", "name"))) and not at(aws, "auth", "secretRef"),
           "namespace SecretStore uses explicit service-account IRSA, without static-key secretRef")
    external = bundle.get("external_secret", {})
    target = at(external, "spec", "target", "name")
    entries = at(external, "spec", "data", default=[])
    token_ref = at(env, "ADMIN_TOKEN", "valueFrom", "secretKeyRef", default={})
    expect("eso.admin_reference", at(external, "kind") == "ExternalSecret" and
           at(external, "metadata", "namespace") == namespace and bool(target) and
           at(external, "spec", "secretStoreRef", "name") == at(store, "metadata", "name") and
           at(external, "spec", "secretStoreRef", "kind") == "SecretStore" and
           at(token_ref, "name") == target and "value" not in env.get("ADMIN_TOKEN", {}) and
           isinstance(entries, list) and any(at(e, "secretKey") == at(token_ref, "key") and
                                           bool(text(at(e, "remoteRef", "key"))) for e in entries),
           "ADMIN_TOKEN comes from the declared ExternalSecret; DB credentials still use the app SDK")
    return report(checks)


def report(checks):
    return {"scope": "local_configuration_only", "configuration_result": "PASS" if all(c["result"] == "PASS" for c in checks) else "BLOCK",
            "runtime_verified": False, "deployment_ready": False, "checks": checks,
            "remaining_runtime_evidence": ["T1 access and network/egress readiness", "EKS/CNPG/operator versions and image digests",
                "CloudTrail active trail and event selectors", "running DB settings, extensions, roles and publication",
                "PgBouncer backend TLS and app traffic", "workload IAM authorization and Secret rotation",
                "T34 login/DDL/RoleBinding canary delivery and read-only log search"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("configuration", type=Path)
    a = p.parse_args()
    try:
        data = json.loads(a.configuration.read_text())
        result = check(data)
    except (OSError, ValueError, TypeError):
        # Do not echo input or a parser excerpt: it may contain credentials.
        result = report([{"id": "input", "result": "BLOCK", "requirement": "readable, well-formed configuration JSON"}])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["configuration_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
