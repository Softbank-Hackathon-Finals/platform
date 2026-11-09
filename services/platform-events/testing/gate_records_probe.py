"""T7 runtime DB -> T10 gate storage. Optional retained source, no target run.

Run only as an owned, bounded cluster Job with the existing runtime DB/CA
mounts. The unique verification-only plan remains as append-only test evidence;
delete only the Job and its owned manifest resources, never shared DB history.
No app database, T14 probe, approval, SHIFT or Executor call is made.
"""

import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from bravo_gate.contract import T5Contract, canonical
from bravo_gate.models import GateContext, RunSnapshot
from psycopg.conninfo import make_conninfo

from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanSpec
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError


def verify(conninfo, probe_id, source=None):
    if not re.fullmatch(r"t10-gate-probe-[a-z0-9-]{8,40}", probe_id):
        raise ValueError("invalid_probe_id")
    checks = {}
    with psycopg.connect(conninfo) as c:
        tls = c.execute("SELECT ssl, version FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
        checks["tls_verified"] = tls[0] is True and tls[1] in {"TLSv1.2", "TLSv1.3"}
        checks["runtime_identity"] = c.execute("SELECT current_user").fetchone()[0] == "platform_runtime"
        checks["restricted_role"] = c.execute("SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole "
            "AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0]
        version = c.execute("SHOW server_version").fetchone()[0]
        schema_version = c.execute("SELECT version FROM bravo_platform.schema_version").fetchall()
    if not all(value is True for value in checks.values()):
        return dict(status="FAIL", stage="runtime_preflight", checks=checks, plan_written=False)
    now = datetime.now(UTC)
    store = PostgresPlanStore(conninfo)
    if source is not None:
        if not isinstance(source, RunSnapshot):
            raise ValueError("invalid_source_snapshot")
        T5Contract().validate(source.document, "control_run")
        if source.document["environment"]["role"] != "source":
            raise ValueError("source_role_required")
    source_doc = source.document if source is not None else None
    plan = PlanSpec(probe_id, "verification-only",
                    source_doc["app"] if source_doc else "gate-storage-probe",
                    source_doc["environment"]["id"] if source_doc else "probe-source",
                    "probe-target", "a" * 64, "b" * 64, profile_id="full", profile_version="1")
    store.register_plan(plan, run_id=probe_id, occurred_at=now.isoformat())
    principal = OperatorPrincipal("gate-probe-operator", plan.tenant_id, int(now.timestamp()),
                                  int(now.timestamp()) + 300, int(now.timestamp()), probe_id)
    access = OperatorAccess(store)

    def grant(permissions, revision):
        return access.set_permissions(plan.plan_id, principal, permissions=permissions,
            valid_after=datetime.fromtimestamp(principal.issued_at, UTC).isoformat(),
            expires_at=(now + timedelta(minutes=5)).isoformat(), expected_revision=revision,
            administration_ref=probe_id, command_key=f"probe-grant-{revision}")

    grant(["read"], 0)

    def authorize(c, actor, current, action):
        # This host's fixed, isolated test identity alone may register probe data.
        # A real producer needs its separately authenticated host policy.
        if actor != principal or action not in {"gate:record", "gate:bind", "gate:read", "gate:evaluate"}:
            return False
        access.require(c, actor, current, "read")
        return True

    records = GateRecords(store, authorize=authorize)
    document = dict(kind="control_run", schema_version="1", run_id="probe-source-run",
        environment=dict(id=plan.source_environment_id, role="source", ref="verification/source"),
        app=plan.workload_id, started_at=now.isoformat(), finished_at=now.isoformat(), results=[
            dict(test_id="ENC-01.1", control_id="ENC-01", method_version="1", expected="allow",
                 observed=None, positive_control="not_applicable", verdict="UNKNOWN",
                 unknown_reason="NOT_RUN", evidence=None)])
    snapshot = source if source is not None else RunSnapshot(document["run_id"], canonical(document))
    first = records.record_run(principal, plan.plan_id, "source", snapshot)
    checks["duplicate_is_idempotent"] = first == records.record_run(principal, plan.plan_id, "source", snapshot)
    records.store = store = PostgresPlanStore(conninfo)
    with store._transaction(write=True, plan_id=plan.plan_id) as c:
        current = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan.plan_id,)).fetchone())
        stored = records.read_run(c, principal, current, snapshot.ref)
        checks["reconnect_original_hash"] = stored.fingerprint == snapshot.fingerprint
        checks["original_verdicts_preserved"] = stored.document["results"] == snapshot.document["results"]
        try:
            records.read_run(c, replace(principal, tenant_id="other-tenant"), current, snapshot.ref)
        except EventError as error:
            checks["other_tenant_denied"] = str(error) == "forbidden"
        else:
            checks["other_tenant_denied"] = False
    ctx = GateContext(plan.tenant_id, plan.workload_id, "probe-gate", plan.plan_digest,
        "full", "1", T5Contract().catalog_digest, plan.source_environment_id,
        snapshot.document["environment"]["ref"], plan.target_environment_id, "verification/target", snapshot.ref,
        "missing-target", snapshot.fingerprint, "c" * 64)
    try:
        records.bind(principal, plan.plan_id, "probe-context", ctx, expected_revision=0)
    except EventError as error:
        checks["missing_target_blocks_binding"] = str(error) == "gate_run_unavailable_or_changed"
    else:
        checks["missing_target_blocks_binding"] = False
    changed = snapshot.document
    changed["run_id"] = "different-run"
    try:
        records.record_run(principal, plan.plan_id, "source", RunSnapshot(snapshot.ref, canonical(changed)))
    except EventError as error:
        checks["same_ref_replacement_denied"] = str(error) == "gate_record_conflict"
    else:
        checks["same_ref_replacement_denied"] = False
    grant([], 1)
    with store._transaction(write=True, plan_id=plan.plan_id) as c:
        try:
            records.read_run(c, principal, current, snapshot.ref)
        except EventError as error:
            checks["durable_revocation_denies_read"] = str(error) == "forbidden"
        else:
            checks["durable_revocation_denies_read"] = False
        keys = [r["command_key"] for r in c.execute("SELECT command_key FROM plan_commands WHERE plan_id=?",
                                                    (plan.plan_id,)).fetchall()]
        checks["no_gate_context_or_report_created"] = not any(k.startswith(("gate-current-", "gate-context-",
                "gate-security-report-", "gate-combined-report-")) for k in keys)
    checks["plan_stays_collect"] = store.snapshot(plan.plan_id)["state"] == "COLLECT"
    return dict(status="PASS" if all(v is True for v in checks.values()) else "FAIL",
                checks=checks, check_count=len(checks), server_version=version,
                state_schema_versions=[row[0] for row in schema_version],
                retained_plan_id=probe_id, source_digest=snapshot.fingerprint,
                scope="cluster_runtime_storage_with_missing_target",
                retained_source_supplied=source is not None,
                source_verdicts={verdict:sum(r["verdict"] == verdict for r in snapshot.document["results"])
                                 for verdict in ("PASS", "FAIL", "UNKNOWN")},
                real_target_result=False, operational_gate_accepted=False, executor_calls=0)


if __name__ == "__main__":
    try:
        conninfo = make_conninfo(host="platform-db-rw.platform-state.svc", port=5432, dbname="platform",
            user=Path("/credential/username").read_text(), password=Path("/credential/password").read_text(),
            sslmode="verify-full", sslrootcert="/ca/ca.crt", connect_timeout=5)
        source = None
        if os.environ.get("BRAVO_PROBE_SOURCE_PATH"):
            raw = Path(os.environ["BRAVO_PROBE_SOURCE_PATH"]).read_bytes()
            from bravo_gate.contract import decode
            source = RunSnapshot(decode(raw)["run_id"], raw)
        result = verify(conninfo, os.environ["BRAVO_PROBE_ID"], source)
        print(json.dumps(result))
        raise SystemExit(0 if result["status"] == "PASS" else 1)
    except Exception as error:
        print(json.dumps(dict(status="FAIL", error_type=type(error).__name__, details_suppressed=True)))
        raise SystemExit(1) from None
