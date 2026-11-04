"""Synthetic T10 consumer probe inside the T7 cluster; no external actions or login API."""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import CollectionProof, PlanSpec
from bravo_platform_events.postgres import PostgresPlanStore
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from tls_verification import verify_plaintext_rejection


def main():
    probe_id = os.environ["BRAVO_PROBE_ID"]
    if not re.fullmatch(r"t7-[a-z0-9-]{1,48}", probe_id):
        raise ValueError("invalid_probe_id")
    conninfo = make_conninfo(host="platform-db-rw.platform-state.svc", port=5432, dbname="platform",
        user=Path("/credential/username").read_text(), password=Path("/credential/password").read_text(),
        sslmode="verify-full", sslrootcert="/ca/ca.crt", connect_timeout=5)
    checks = {}
    with psycopg.connect(conninfo) as c:
        row = c.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
        checks["tls_verified"] = row[0] is True and row[1] in ("TLSv1.2", "TLSv1.3")
        checks["runtime_identity"] = c.execute("SELECT current_user").fetchone()[0] == "platform_runtime"
        checks["not_superuser"] = c.execute("SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0]
        version = c.execute("SHOW server_version").fetchone()[0]
    spec = PlanSpec(probe_id, "tenant-t7-probe", "workload-t7-probe", "source-probe", "target-probe", "a" * 64, "b" * 64)
    proof = CollectionProof(probe_id, spec.plan_digest, spec.collection_scope_digest, "evidence-t7-probe", True)
    now = "2026-11-03T10:00:00Z"
    def complete(_=None):
        return PostgresPlanStore(conninfo).complete_collection(proof=proof, expected_version=1,
            command_key="collect-probe", run_id="run-t7-probe", occurred_at=now)
    store = PostgresPlanStore(conninfo)
    store.register_plan(spec, run_id="run-t7-probe", occurred_at=now)
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(complete, range(8)))
    checks["concurrent_command_idempotency"] = all(r == receipts[0] for r in receipts)
    event = store.append(plan_id=probe_id, run_id="run-t7-probe", producer_key="progress-probe",
        event_type="execution.updated", occurred_at=now,
        data={"execution_id": "synthetic-probe", "step": "collect", "status": "succeeded"})
    reopened = PostgresPlanStore(conninfo)
    snapshot = reopened.snapshot(probe_id)
    checks["store_reopen_preserves_state"] = snapshot["state"] == "ANALYZE" and snapshot["version"] == 2
    checks["state_event_watermark"] = snapshot["last_event_id"] == event["event_id"]
    checks["events_contiguous"] = [e["sequence"] for e in reopened.read_after(probe_id, None)] == [1, 2, 3]
    checks["original_receipt_preserved"] = complete() == receipts[0]
    # Authenticated principal is a probe fixture inside this process, never a network host.
    app = create_plan_app(reopened, resolve_principal=lambda _: Principal("t7-probe", spec.tenant_id))
    with TestClient(app) as client:
        response = client.get(f"/v1/plans/{probe_id}")
        checks["http_snapshot"] = response.status_code == 200 and response.json()["state"] == "ANALYZE"
        replay = client.get(f"/v1/plans/{probe_id}/events?follow=false", headers={"Last-Event-ID": f"{probe_id}:1"})
        checks["http_sse_reconnect"] = replay.status_code == 200 and replay.text.count("\nid:") + replay.text.startswith("id:") == 2
    with TestClient(create_plan_app(reopened)) as client:
        checks["http_no_identity_denied"] = client.get(f"/v1/plans/{probe_id}").status_code == 403
    with TestClient(create_plan_app(reopened, resolve_principal=lambda _: Principal("other", "other-tenant"))) as client:
        checks["http_other_tenant_denied"] = client.get(f"/v1/plans/{probe_id}").status_code == 403
    statements = {
        "ddl": "CREATE TABLE bravo_platform.forbidden_probe (id INTEGER)",
        "public_ddl": "CREATE TABLE public.forbidden_probe (id INTEGER)",
        "schema_create": "CREATE SCHEMA forbidden_probe",
        "temp_table": "CREATE TEMP TABLE forbidden_probe (id INTEGER)",
        "tenant_rebind": "UPDATE bravo_platform.plans SET tenant_id='other'",
        "spec_rewrite": "UPDATE bravo_platform.plans SET spec_json='{}'",
        "event_rewrite": "UPDATE bravo_platform.events SET event_json='{}'",
        "event_delete": "DELETE FROM bravo_platform.events",
        "receipt_delete": "DELETE FROM bravo_platform.plan_commands",
        "schema_version": "UPDATE bravo_platform.schema_version SET version=1",
        "migration_escalation": "SET ROLE bravo_platform_migrator",
        "role_grant": "GRANT bravo_platform_migrator TO platform_runtime",
    }
    for name, statement in statements.items():
        with psycopg.connect(conninfo) as c:
            try:
                c.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                checks["denied_" + name] = True
            else:
                # Abort instead of committing even if a broken privilege allowed the probe.
                raise AssertionError("privilege_boundary_failed")
    checks["history_unchanged_after_denials"] = reopened.snapshot(probe_id) == snapshot
    plaintext = verify_plaintext_rejection(conninfo, psycopg.connect, psycopg.OperationalError)
    checks["plaintext_denied"] = {"PASS": True, "FAIL": False, "UNKNOWN": None}[plaintext["verdict"]]
    status = "FAIL" if any(value is False for value in checks.values()) else "UNKNOWN" if None in checks.values() else "PASS"
    result = {"status": status, "server_version": version, "plaintext": plaintext,
              "checks": checks, "check_count": len(checks),
              "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
              "receipt_sha256": hashlib.sha256(json.dumps(receipts[0], sort_keys=True).encode()).hexdigest(),
              "scope": "synthetic_T10_storage_and_ASGI_only"}
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (psycopg.Error, OSError, ValueError, KeyError, RuntimeError, AssertionError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "details_suppressed": True}))
        raise SystemExit(1) from None
