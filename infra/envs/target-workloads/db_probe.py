"""Bounded destination TLS/role probe. Credentials come only from Secret-backed env."""
import json
import os
import socket
import subprocess


def main():
    host = "target-db-rw.target-dev.svc"
    base = {**os.environ, "PGHOST": host, "PGPORT": "5432", "PGDATABASE": "chat",
            "PGUSER": "chat_replicator", "PGPASSWORD": os.environ["REPLICATION_PASSWORD"],
            "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/tls/ca.crt", "PGCONNECT_TIMEOUT": "10"}

    def query(sql, *, env=None, conninfo=None):
        args = ["psql", "-XAt", "--set=ON_ERROR_STOP=1", "--set=VERBOSITY=sqlstate"]
        if conninfo:
            args.extend(["--dbname", conninfo])
        return subprocess.run(args, input=sql.encode(), capture_output=True,
                              env={**base, **(env or {})}, timeout=30)

    checks = {}
    result = query("SELECT current_user || '|' || ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid();")
    checks["scram_role_tls_connection"] = result.returncode == 0 and result.stdout.strip() == b"chat_replicator|true"
    result = query("SELECT count(*) FROM messages; SELECT count(*) FROM attachments;")
    checks["replicator_table_select"] = result.returncode == 0
    result = query("SELECT bool_and(NOT has_table_privilege(current_user, t, p)) "
                   "FROM unnest(ARRAY['public.messages','public.attachments']) AS t "
                   "CROSS JOIN unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE']) AS p;")
    checks["both_tables_all_write_privileges_absent"] = result.returncode == 0 and result.stdout.strip() == b"t"
    result = query("SELECT bool_and(NOT has_any_column_privilege(current_user, t, 'INSERT,UPDATE')) "
                   "FROM unnest(ARRAY['public.messages','public.attachments']) AS t;")
    checks["both_tables_column_write_privileges_absent"] = result.returncode == 0 and result.stdout.strip() == b"t"
    result = query("IDENTIFY_SYSTEM;", conninfo="dbname=chat replication=database")
    checks["logical_replication_allowed"] = result.returncode == 0
    result = query("IDENTIFY_SYSTEM;", conninfo="replication=true")
    checks["physical_replication_denied"] = result.returncode != 0 and any(
        text in result.stderr for text in [b"no pg_hba.conf entry", b"pg_hba.conf rejects connection"])
    result = query("BEGIN; INSERT INTO messages(idempotency_key,author,body) VALUES "
                   "('1207fe01-5bb5-4d9d-bb59-4e12a701d5c6','T8 negative probe','must roll back'); ROLLBACK;")
    checks["replicator_write_denied"] = result.returncode != 0 and b"42501" in result.stderr
    result = query("SELECT 1;", env={"PGPASSWORD": "intentionally-invalid-probe-password"})
    checks["wrong_password_denied"] = result.returncode != 0 and b"password authentication failed" in result.stderr
    result = query("SELECT 1;", env={"PGSSLMODE": "disable"})
    checks["plaintext_denied"] = result.returncode != 0 and b"pg_hba.conf rejects connection" in result.stderr
    result = query("SELECT 1;", env={"PGHOST": "wrong-server.invalid", "PGHOSTADDR": socket.gethostbyname(host)})
    checks["wrong_tls_hostname_denied"] = result.returncode != 0 and b"does not match host name" in result.stderr
    result = query("SELECT 1;", env={"PGUSER": "chat_app", "PGPASSWORD": os.environ["APP_PASSWORD"]})
    checks["application_login_closed"] = result.returncode != 0 and b"is not permitted to log in" in result.stderr
    print(json.dumps({"checks": checks, "pass": all(checks.values()), "raw_output_suppressed": True}))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"pass": False, "error_category": type(error).__name__, "raw_output_suppressed": True}))
        raise SystemExit(1) from None
