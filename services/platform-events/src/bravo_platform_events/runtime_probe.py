"""Bounded operator probe of an already migrated PostgreSQL store.

No migration, infrastructure changes, restart, deletion or arbitrary SQL input.
The only retained data is one synthetic plan per invocation. Credentials are read
from BRAVO_PLATFORM_DATABASE_URL and never emitted or included in the private checkpoint.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from jsonschema import ValidationError
from psycopg.conninfo import conninfo_to_dict

from .plans import CollectionProof, PlanSpec
from .postgres import PostgresPlanStore
from .store import EventError


class ProbeFailure(RuntimeError):
    """A fixed public code, never driver/credential diagnostics."""


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def connection_checks(conninfo):
    """Check the actual runtime identity and encrypted connection before writing."""
    options = conninfo_to_dict(conninfo)
    require(
        options.get("sslmode") == "verify-full"
        and all(options.get(key) for key in ("host", "dbname", "user", "sslrootcert"))
        and not any(char in options["host"] for char in ("/", ",", " ")),
        "verified_tls_connection_required",
    )
    with psycopg.connect(conninfo, connect_timeout=5) as c:
        c.execute("SET TRANSACTION READ ONLY")
        c.execute("SET LOCAL search_path = pg_catalog")
        c.execute("SET LOCAL statement_timeout = '10s'")
        identity = c.execute("SELECT current_user, current_database()").fetchone()
        require(identity == (options["user"], options["dbname"]), "runtime_identity_mismatch")
        tls = c.execute(
            "SELECT ssl FROM pg_catalog.pg_stat_ssl WHERE pid = pg_backend_pid()"
        ).fetchone()
        require(tls == (True,), "encrypted_session_required")
        role = c.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone()
        require(role == (False,) * 5, "runtime_role_excess_privilege")
        membership = c.execute(
            "SELECT pg_has_role(current_user, 'bravo_platform_runtime', 'USAGE'), "
            "pg_has_role(current_user, 'bravo_platform_migrator', 'MEMBER'), "
            "has_database_privilege(current_user, current_database(), 'CREATE'), "
            "has_schema_privilege(current_user, 'bravo_platform', 'CREATE'), "
            "has_schema_privilege(current_user, 'public', 'CREATE')"
        ).fetchone()
        require(membership == (True, False, False, False, False), "runtime_role_boundary")
        groups = c.execute(
            "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname <> current_user "
            "AND pg_has_role(current_user, oid, 'MEMBER') ORDER BY rolname"
        ).fetchall()
        require(groups == [("bravo_platform_runtime",)], "unexpected_runtime_membership")
        tables = ["schema_version", "streams", "state_owners", "events", "plans", "plan_commands"]
        ownership = c.execute(
            "SELECT count(*), bool_and(tableowner = 'bravo_platform_migrator') "
            "FROM pg_catalog.pg_tables WHERE schemaname = 'bravo_platform' "
            "AND tablename = ANY(%s)", (tables,),
        ).fetchone()
        require(ownership == (6, True), "storage_ownership_mismatch")
        for table in tables:
            privileges = c.execute(
                "SELECT has_table_privilege(current_user, %s, 'DELETE'), "
                "has_table_privilege(current_user, %s, 'TRUNCATE'), "
                "has_table_privilege(current_user, %s, 'TRIGGER')",
                ("bravo_platform." + table,) * 3,
            ).fetchone()
            require(privileges == (False,) * 3, "runtime_destructive_privilege")
        return {
            "tls_verify_full": True,
            "encrypted_session": True,
            "runtime_identity_matched": True,
            "runtime_role_boundary": True,
            "migration_owned_tables": True,
            "destructive_table_privileges_absent": True,
            "server_version_num": c.info.server_version,
        }


def context():
    return {
        "plan_id": "pg-probe-" + uuid4().hex,
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def inputs(value):
    require(
        isinstance(value, dict)
        and set(value) == {"plan_id", "occurred_at"}
        and isinstance(value["plan_id"], str)
        and re.fullmatch(r"pg-probe-[a-f0-9]{32}", value["plan_id"])
        and isinstance(value["occurred_at"], str),
        "invalid_probe_checkpoint",
    )
    spec = PlanSpec(
        plan_id=value["plan_id"],
        tenant_id="probe-tenant",
        workload_id="probe-workload",
        source_environment_id="probe-source",
        target_environment_id="probe-target",
        plan_digest=hashlib.sha256(b"synthetic probe plan").hexdigest(),
        collection_scope_digest=hashlib.sha256(b"synthetic probe scope").hexdigest(),
    )
    proof = CollectionProof(
        plan_id=spec.plan_id,
        plan_digest=spec.plan_digest,
        collection_scope_digest=spec.collection_scope_digest,
        evidence_id="probe-evidence",
        complete=True,
    )
    return spec, proof


def complete(store, value, proof):
    return store.complete_collection(
        proof, expected_version=1, command_key="probe-collection", run_id="probe-run",
        occurred_at=value["occurred_at"],
    )


def retained(store, value):
    """Never recreate a missing plan/receipt during a reconnect or recovery check."""
    spec, proof = inputs(value)
    snapshot = store.snapshot(spec.plan_id)
    require(
        snapshot["state"] == "ANALYZE"
        and snapshot["version"] == 2
        and snapshot["last_event_id"] == spec.plan_id + ":4"
        and snapshot["created_at"] == value["occurred_at"]
        and snapshot["updated_at"] == value["occurred_at"],
        "retained_snapshot_mismatch",
    )
    expected_spec = asdict(spec)
    expected_spec.pop("tenant_id")
    require(all(snapshot.get(k) == v for k, v in expected_spec.items()), "retained_spec_mismatch")
    require(store.tenant_for(spec.plan_id) == spec.tenant_id, "retained_tenant_mismatch")
    events = store.read_after(spec.plan_id, None)
    require([e["sequence"] for e in events] == [1, 2, 3, 4], "retained_sequence_mismatch")
    require(
        [e["event_type"] for e in events] == [
            "state.changed", "state.changed", "execution.updated", "execution.updated"
        ],
        "retained_events_mismatch",
    )
    require(
        all(e["plan_id"] == spec.plan_id and e["run_id"] == "probe-run"
            and e["occurred_at"] == value["occurred_at"]
            and e["event_id"] == f"{spec.plan_id}:{index}"
            for index, e in enumerate(events, 1))
        and events[0]["data"] == {"from": None, "to": "COLLECT"}
        and events[1]["data"] == {"from": "COLLECT", "to": "ANALYZE"}
        and sorted(e["data"]["execution_id"] for e in events[2:])
        == ["probe-execution-0", "probe-execution-1"]
        and all(e["data"] == {"execution_id": e["data"]["execution_id"],
                              "step": "probe", "status": "succeeded"} for e in events[2:]),
        "retained_event_content_mismatch",
    )
    require(store.read_after(spec.plan_id, spec.plan_id + ":2") == events[2:],
            "retained_replay_mismatch")
    receipt = complete(store, value, proof)
    require(
        receipt == dict(plan_id=spec.plan_id, state="ANALYZE", version=2,
                        event_id=spec.plan_id + ":2"),
        "retained_receipt_mismatch",
    )
    require(store.snapshot(spec.plan_id) == snapshot, "retry_changed_snapshot")
    require(store.read_after(spec.plan_id, spec.plan_id + ":4") == [], "watermark_mismatch")
    return {"retained_state_events_receipt": True, "immutable_tenant": True,
            "contiguous_event_count": 4, "snapshot_watermark": True, "original_receipt_retry": True}


def exercise(store_factory, value):
    """Only synthetic data through the existing adapter; no direct SQL mutation."""
    spec, proof = inputs(value)
    store = store_factory()
    require(store.tenant_for(spec.plan_id) is None, "probe_plan_already_exists")

    def register(_):
        store_factory().register_plan(spec, run_id="probe-run", occurred_at=value["occurred_at"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(register, range(4)))
    before = store.snapshot(spec.plan_id)
    require(before["last_event_id"] == spec.plan_id + ":1", "registration_not_idempotent")
    try:
        store.complete_collection(
            proof, expected_version=1, command_key="probe-invalid", run_id="probe-run",
            occurred_at="invalid-timestamp",
        )
    except ValidationError:
        pass
    else:
        raise ProbeFailure("invalid_event_accepted")
    require(store.snapshot(spec.plan_id) == before, "failed_write_not_rolled_back")
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: complete(store_factory(), value, proof), range(4)))
    require(all(r == receipts[0] for r in receipts), "command_retry_mismatch")

    def append(index):
        return store_factory().append(
            plan_id=spec.plan_id, run_id="probe-run", producer_key=f"probe-progress-{index}",
            event_type="execution.updated", occurred_at=value["occurred_at"],
            data={"execution_id": f"probe-execution-{index}", "step": "probe", "status": "succeeded"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        events = list(pool.map(append, range(2)))
    require(sorted(e["sequence"] for e in events) == [3, 4], "append_sequence_mismatch")
    require(append(0) == events[0], "append_retry_mismatch")
    try:
        store.read_after(spec.plan_id, "different-probe:1")
    except EventError as error:
        require(error.code == "invalid_cursor", "unexpected_cursor_error")
    else:
        raise ProbeFailure("cross_plan_cursor_accepted")
    return {**retained(store_factory(), value), "concurrent_registration": True,
            "concurrent_command_retry": True, "concurrent_append": True,
            "failed_write_rollback": True, "cross_plan_cursor_denied": True}


def permission_checks(conninfo, plan_id):
    """Expected 42501, not missing objects. Always roll back, even if allowed."""
    statements = {
        "tenant_update_denied": "UPDATE bravo_platform.plans SET tenant_id='forbidden' WHERE plan_id=%s",
        "spec_update_denied": "UPDATE bravo_platform.plans SET spec_json='{}' WHERE plan_id=%s",
        "event_update_denied": "UPDATE bravo_platform.events SET event_json='{}' WHERE plan_id=%s",
        "receipt_update_denied": "UPDATE bravo_platform.plan_commands SET receipt_json='{}' WHERE plan_id=%s",
    }
    result = {}
    for key, statement in statements.items():
        with psycopg.connect(conninfo, connect_timeout=5) as c:
            try:
                c.execute("SET LOCAL statement_timeout = '10s'")
                c.execute(statement, (plan_id,))
            except psycopg.errors.InsufficientPrivilege:
                result[key] = True
            finally:
                c.rollback()
        require(result.get(key) is True, "runtime_unexpected_write_privilege")
    return result


def read_checkpoint(path):
    require(
        not path.is_symlink() and path.stat().st_uid == os.getuid()
        and path.stat().st_mode & 0o077 == 0,
        "checkpoint_must_be_private",
    )
    value = json.loads(path.read_text())
    require(value.get("format_version") == 1 and set(value) == {"format_version", "context"},
            "invalid_probe_checkpoint")
    inputs(value["context"])
    return value["context"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("exercise", "verify"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        conninfo = os.environ.get("BRAVO_PLATFORM_DATABASE_URL", "")
        require(bool(conninfo), "runtime_connection_missing")
        checks = connection_checks(conninfo)
        if args.phase == "exercise":
            value = context()
            # Persist deterministic retry inputs before the first DB write.
            with args.checkpoint.open("x") as output:
                json.dump({"format_version": 1, "context": value}, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            checks.update(exercise(lambda: PostgresPlanStore(conninfo), value))
        else:
            value = read_checkpoint(args.checkpoint)
            checks.update(retained(PostgresPlanStore(conninfo), value))
        checks.update(permission_checks(conninfo, value["plan_id"]))
        print(json.dumps({
            "status": "PASS", "phase": args.phase, "checks": checks,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_sha256": {
                name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                for name in ("runtime_probe.py", "postgres.py", "plans.py", "store.py", "contract.py",
                             "schemas/event.schema.json")
            },
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "psycopg_version": psycopg.__version__,
            "checkpoint_sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
            "synthetic_plan_retained": True, "database_restart_performed": False,
            "backup_restore_verified": False, "http_login_or_deployment_verified": False,
        }))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ProbeFailure) else "probe_failed"
        print(json.dumps({"status": "FAIL", "error_code": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
