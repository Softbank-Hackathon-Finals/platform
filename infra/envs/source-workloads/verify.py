#!/usr/bin/env python3
"""Collect selected live T6 facts without exporting credentials or raw logs.

--canary creates only named audit objects in the new source-dev namespace and
t6_audit_canary DB schema. It never alters application data or credentials.
"""

import argparse
import base64
import json
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

NS = "source-dev"
SQL = """SELECT json_build_object(
'server_version',current_setting('server_version'),
'wal_level',current_setting('wal_level'),
'log_connections',current_setting('log_connections'),
'pgaudit_log',current_setting('pgaudit.log'),
'pgaudit_log_parameter',current_setting('pgaudit.log_parameter'),
'pgaudit_extension',EXISTS(SELECT 1 FROM pg_extension WHERE extname='pgaudit'),
'publication_tables',(SELECT json_agg(tablename ORDER BY tablename) FROM pg_publication_tables WHERE pubname='source_chat'),
'app_superuser',(SELECT rolsuper FROM pg_roles WHERE rolname='chat_app'),
'app_schema_create',has_schema_privilege('chat_app','public','CREATE'),
'replication_role',(SELECT rolreplication FROM pg_roles WHERE rolname='chat_replicator'),
'pooler_control_is_db_role',EXISTS(SELECT 1 FROM pg_roles WHERE rolname='cutover_control'),
'tables_without_pk',(SELECT count(*) FROM pg_tables t WHERE schemaname='public' AND NOT EXISTS(SELECT 1 FROM pg_constraint c WHERE c.conrelid=(quote_ident(t.schemaname)||'.'||quote_ident(t.tablename))::regclass AND c.contype='p'))
);"""


def ddl_canary_observed(logs):
    """CNPG parses pgAudit CSV into record.audit; ordinary SQL text is not proof."""
    for line in logs.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        record = event.get("record", {})
        audit = record.get("audit", {})
        if (event.get("logger") == "pgaudit" and record.get("database_name") == "chat"
                and audit.get("class") == "DDL"
                and "t6_audit_canary" in audit.get("statement", "")):
            return True
    return False


class Probe:
    def __init__(self, kubeconfig, cluster, region):
        self.kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace", NS]
        self.aws = ["aws", "--region", region, "--no-cli-pager", "--output", "json"]
        self.cluster = cluster

    @staticmethod
    def run(command, payload=None, check=True):
        result = subprocess.run(command, input=payload, capture_output=True, text=True,
                                timeout=60, check=False)
        if check and result.returncode:
            raise RuntimeError("runtime read failed; provider output suppressed")
        return result

    def get(self, *args):
        return json.loads(self.run(self.kube + ["get", *args, "-o", "json"]).stdout)

    def secret(self, name):
        return {k: base64.b64decode(v).decode() for k, v in self.get("secret", name)["data"].items()}

    def admin_sql(self, primary, query):
        return self.run(self.kube + ["exec", primary, "-c", "postgres", "--", "psql",
                        "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "chat", "-c", query]).stdout

    def pooler_sql(self, user, password, database, query, tls="verify-full", app_name="t6-readback"):
        # Only a password travels over stdin. SQL and non-secret options are args.
        script = ('IFS= read -r PGPASSWORD; export PGPASSWORD; '
                  'export PGSSLMODE="$1" PGSSLROOTCERT=/etc/cnpg-ca/ca.crt PGAPPNAME="$2"; '
                  'exec psql -XAt -v ON_ERROR_STOP=1 -h source-pgbouncer.source-dev.svc.cluster.local '
                  '-p 5432 -U "$3" -d "$4" -c "$5"')
        return self.run(self.kube + ["exec", "-i", "deployment/source-pgbouncer", "--", "sh", "-c",
                                    script, "sh", tls, app_name, user, database, query], password + "\n", False)

    def audit_canary(self, primary):
        self.admin_sql(primary, "CREATE SCHEMA IF NOT EXISTS t6_audit_canary; "
                       "CREATE TABLE IF NOT EXISTS t6_audit_canary.connection_probe (id integer PRIMARY KEY);")
        objects = []
        for kind, spec in [
            ("ServiceAccount", {"automountServiceAccountToken": False}),
            ("Role", {"rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}]}),
            ("RoleBinding", {"roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "t6-audit-canary"},
                             "subjects": [{"kind": "ServiceAccount", "name": "t6-audit-canary", "namespace": NS}]})]:
            objects.append({"apiVersion": "v1" if kind == "ServiceAccount" else "rbac.authorization.k8s.io/v1",
                            "kind": kind, "metadata": {"name": "t6-audit-canary", "namespace": NS,
                                "labels": {"app.kubernetes.io/part-of": "bravo-source", "component": "source"}}, **spec})
        self.run(self.kube + ["apply", "--server-side", "-f", "-"],
                 json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))

    def collect(self, canary=False):
        observed = datetime.now(timezone.utc)
        result = {"observed_at": observed.isoformat(), "scope": "live_source_workload_readback",
                  "secret_values_exported": False, "canary_requested": canary}
        cluster = self.get("cluster", "source-db")
        primary = cluster.get("status", {}).get("currentPrimary")
        if not primary:
            raise RuntimeError("database primary not ready")
        result["database_ready_instances"] = cluster["status"].get("readyInstances", 0)
        result["database"] = json.loads(self.admin_sql(primary, SQL))
        result["pvc_phases"] = [p["status"]["phase"] for p in self.get("pvc")["items"]]
        result["deployments"] = [{"name": d["metadata"]["name"],
                                  "desired": d["spec"]["replicas"],
                                  "ready": d.get("status", {}).get("readyReplicas", 0)}
                                 for d in self.get("deployments")["items"]]
        for kind in ("secretstore", "externalsecret"):
            result[kind] = [{"name": x["metadata"]["name"], "ready": any(
                c["type"] == "Ready" and c["status"] == "True" for c in x.get("status", {}).get("conditions", []))}
                for x in self.get(kind)["items"]]
        app = self.secret("source-app-db")
        control = self.secret("source-control-bootstrap")
        login_name = "t6-canary-" + observed.strftime("%Y%m%dT%H%M%SZ")
        read = self.pooler_sql("chat_app", app["password"], "chat",
            "SELECT json_build_object('current_user',current_user,'tls',(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),'tls_version',(SELECT version FROM pg_stat_ssl WHERE pid=pg_backend_pid()),'message_count',(SELECT count(*) FROM messages))", app_name=login_name)
        result["pooler_tls_read"] = json.loads(read.stdout) if read.returncode == 0 else {"connected": False}
        denied = self.pooler_sql("chat_app", app["password"], "pgbouncer", "SHOW DATABASES")
        result["app_pooler_admin_denied"] = denied.returncode != 0 and "not allowed" in denied.stderr
        allowed = self.pooler_sql("cutover_control", control["pgbouncer_password"], "pgbouncer", "SHOW DATABASES")
        result["control_pooler_admin_allowed"] = allowed.returncode == 0
        wrong = self.pooler_sql("chat_app", secrets.token_urlsafe(32), "chat", "SELECT 1")
        result["wrong_password_denied"] = wrong.returncode != 0 and "authentication" in wrong.stderr
        plain = self.pooler_sql("chat_app", app["password"], "chat", "SELECT 1", tls="disable")
        result["plaintext_client_denied"] = plain.returncode != 0 and "SSL" in plain.stderr
        ddl = self.pooler_sql("chat_app", app["password"], "chat",
                             "BEGIN; CREATE TABLE public.t6_permission_probe(id integer); ROLLBACK")
        result["app_ddl_denied"] = ddl.returncode != 0 and "permission denied" in ddl.stderr
        replication = self.secret("source-replication")
        replication_command = (
            'IFS= read -r PGPASSWORD; export PGPASSWORD; '
            'export PGSSLMODE=verify-full PGSSLROOTCERT=/etc/cnpg-ca/ca.crt '
            'PGHOST=source-db-rw.source-dev.svc.cluster.local PGPORT=5432 PGUSER=chat_replicator; '
            'exec psql -XAt -v ON_ERROR_STOP=1 "$1" -c IDENTIFY_SYSTEM')
        for mode, key in [("database", "logical_replication_startup_allowed"),
                          ("true", "physical_replication_startup_denied")]:
            probe = self.run(self.kube + ["exec", "-i", "deployment/source-pgbouncer", "--", "sh", "-c",
                replication_command, "sh", "dbname=chat replication=" + mode], replication["password"] + "\n", False)
            result[key] = (probe.returncode == 0 if mode == "database"
                           else probe.returncode != 0 and "pg_hba.conf" in probe.stderr)
        if canary:
            self.audit_canary(primary)
        logs = self.run(self.kube + ["logs", primary, "-c", "postgres", "--since=20m"]).stdout
        result["db_ddl_canary_observed"] = ddl_canary_observed(logs)
        result["db_connection_observed"] = any("connection authorized" in line and "chat_app" in line for line in logs.splitlines())
        raw = self.run(self.aws + ["logs", "filter-log-events", "--log-group-name",
                       f"/aws/eks/{self.cluster}/cluster", "--filter-pattern", '"t6-audit-canary"',
                       "--start-time", str(int(observed.timestamp() - 1200) * 1000)]).stdout
        events = [json.loads(e["message"]) for e in json.loads(raw).get("events", [])]
        result["eks_rolebinding_canary_observed"] = any(
            e.get("objectRef", {}).get("resource") == "rolebindings" and
            e.get("objectRef", {}).get("name") == "t6-audit-canary" and
            e.get("responseStatus", {}).get("code", 999) < 300 for e in events)
        trail = json.loads(self.run(self.aws + ["cloudtrail", "get-trail-status", "--name", "bravo-source-workloads"]).stdout)
        result["cloudtrail"] = {"is_logging": trail.get("IsLogging", False),
                                "delivery_error": bool(trail.get("LatestDeliveryError")),
                                "delivery_observed": bool(trail.get("LatestDeliveryTime"))}
        result["limitations"] = ["No T8 cross-environment replication or T14 equivalence claim",
                                 "No T35 backup/restore or multi-node availability claim",
                                 "Application API and S3 read/write verification are reported separately"]
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True, type=Path)
    p.add_argument("--cluster", default="bravo-source-runtime")
    p.add_argument("--region", default="ap-northeast-2")
    p.add_argument("--canary", action="store_true")
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    try:
        report = Probe(args.kubeconfig, args.cluster, args.region).collect(args.canary)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        print(json.dumps({"readback": "BLOCK", "raw_provider_output_suppressed": True}))
        raise SystemExit(1) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
