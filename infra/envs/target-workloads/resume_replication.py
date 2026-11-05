#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.6"]
# ///
"""Resume only the forward subscription after it turned itself off, then prove it caught up.

The default writes nothing: read-only checks, and if the subscription is already on, the read-only
post-check as well. --apply adds at most two things, and only when every check passed: one ENABLE
of the forward subscription if it is off, and one test row in cutover.barrier that is deleted again.
A running subscription is never enabled a second time. Slots, roles, the reverse subscription and the
application's write block are never touched, and nothing is ever written to the target. The test row
goes only to the database verified as the source, and --cleanup-marker finishes removing one a failed
run reported as pending.

The output uses the names and units of the first actual resume (docs/evidence/t8-forward-recovery-2026-11-04,
PR #201), so that a later run reads the same way. Rows are hashed only up to the source snapshot taken
first, because the source may still be writing; the hashes themselves are never printed.

Connections come only from the environment. Output is one JSON document without DSNs, hosts or
driver messages. Exit 0 means the checks passed; anything not confirmed exits 1.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().with_name("testing")))

from attachment_copy import AttachmentCopyFailed, require_verified_tls, tls_settings
from lag_metrics import parse_lsn
from lag_readonly import NotReadOnly, ReadOnlySession  # noqa: F401 - Part of what callers catch.
from replication import names, runtime_state_sql, subscription

SOURCE_DSN_ENV = "T8_RESUME_SOURCE_DSN"
TARGET_DSN_ENV = "T8_RESUME_TARGET_DSN"
EXECUTOR_DSN_ENV = "T8_RESUME_EXECUTOR_DSN"
# Its own variable, so that the read DSN can never write: needs INSERT, SELECT and DELETE on cutover.barrier.
PROBE_DSN_ENV = "T8_RESUME_SOURCE_PROBE_DSN"
# Local test databases only. Applies when host and hostaddr are both literal loopback addresses.
LOOPBACK_ENV = "T8_RESUME_ALLOW_LOOPBACK_PLAINTEXT"
FORWARD = names("target")[2]
REVERSE = names("source")[2]
PUBLICATION = subscription("target")["spec"]["publicationName"]
APPLICATION_NAME = "t8_resume"
# timezone: a row's text, and so its hash, must not depend on either server's default.
TRUSTED_SEARCH_PATH = "-c search_path=pg_catalog,pg_temp"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=120000 -c timezone=UTC " + TRUSTED_SEARCH_PATH
CONTROL_OPTIONS = "-c statement_timeout=10000 -c lock_timeout=5000 " + TRUSTED_SEARCH_PATH
# bravo_cutover.contract.LEASE_HOLDING: an execution in one of these still owns the databases.
LEASE_HOLDING = ("PENDING", "RUNNING", "SAFE_HOLD")
USABLE_WAL_STATUS = ("reserved", "extended")
CURRENT_DATABASE = "(SELECT oid FROM pg_database WHERE datname = pg_catalog.current_database())"
# Catalog joins, not to_regclass: naming a schema the role has no USAGE on raises instead of returning NULL.
BARRIER_TABLE = """(SELECT c.oid FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'cutover' AND c.relname = 'barrier')"""
BARRIER_SCHEMA_USABLE = """EXISTS (SELECT FROM pg_namespace WHERE nspname = 'cutover'
                                   AND has_schema_privilege(current_user, oid, 'USAGE'))"""
BARRIER_READABLE = f"coalesce(has_table_privilege(current_user, {BARRIER_TABLE}, 'SELECT') AND {BARRIER_SCHEMA_USABLE}, false)"
STATEMENTS = {
    "source_status": f"""
        SELECT json_build_object(
            'source_observed_at', now(),
            'system_identifier', (SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
            'database', pg_catalog.current_database(), 'slot_database', s.database,
            'in_recovery', pg_is_in_recovery(),
            'source_wal_flush_lsn', CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_flush_lsn()::text END,
            'barrier_published', EXISTS (SELECT FROM pg_publication_tables WHERE pubname = %(publication)s
                                         AND schemaname = 'cutover' AND tablename = 'barrier'),
            'barrier_readable', {BARRIER_READABLE},
            'source_app_login', (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'),
            'control_sessions', (SELECT count(*) FROM pg_stat_activity
                                 WHERE usename = 'cutover_control' AND application_name <> 't8_resume'),
            'source_wal_sender_timeout_ms', (SELECT setting::bigint FROM pg_settings WHERE name = 'wal_sender_timeout'),
            'max_slot_wal_keep_size', current_setting('max_slot_wal_keep_size'),
            'slot_exists', s.slot_name IS NOT NULL, 'slot_type', s.slot_type,
            'source_slot_active', s.active, 'source_slot_wal_status', s.wal_status,
            'source_slot_invalidated', s.invalidation_reason IS NOT NULL OR s.wal_status = 'lost',
            'invalidation_reason', s.invalidation_reason, 'safe_wal_size', s.safe_wal_size,
            'source_slot_confirmed_flush_lsn', s.confirmed_flush_lsn::text,
            'source_sender_state', w.state, 'source_sender_tls', t.ssl,
            'reverse_enabled', coalesce((SELECT e.subenabled FROM pg_subscription AS e
                                         WHERE e.subname = %(reverse)s AND e.subdbid = {CURRENT_DATABASE}), false),
            'reverse_workers', (SELECT count(a.pid) FROM pg_stat_subscription AS a WHERE a.subname = %(reverse)s))
        FROM (SELECT 1) AS one
        LEFT JOIN pg_replication_slots AS s ON s.slot_name = %(forward)s
        LEFT JOIN pg_stat_replication AS w ON w.pid = s.active_pid
        LEFT JOIN pg_stat_ssl AS t ON t.pid = s.active_pid""",
    # One snapshot, so the bounds of the hashed rows are consistent with each other.
    "source_rows": """
        SELECT json_build_object(
            'messages', count(*), 'messages_max_id', max(id),
            'attachments', (SELECT count(*) FROM public.attachments),
            'attachments_max_created_at', (SELECT max(created_at) FROM public.attachments))
        FROM public.messages""",
    "target_status": f"""
        SELECT json_build_object(
            'target_observed_at', now(),
            'system_identifier', (SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
            'database', pg_catalog.current_database(),
            'in_recovery', pg_is_in_recovery(),
            'target_app_login', (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'),
            'app_sessions', (SELECT count(*) FROM pg_stat_activity
                             WHERE usename = 'chat_app' AND pid <> pg_backend_pid()),
            'control_sessions', (SELECT count(*) FROM pg_stat_activity
                                 WHERE usename = 'cutover_control' AND application_name <> 't8_resume'),
            'target_wal_receiver_timeout_ms', (SELECT setting::bigint FROM pg_settings WHERE name = 'wal_receiver_timeout'),
            'reverse_slot_active', (SELECT active FROM pg_replication_slots WHERE slot_name = %(reverse)s),
            'barrier_readable', {BARRIER_READABLE},
            'origin_readable', has_table_privilege(current_user, 'pg_catalog.pg_replication_origin_status', 'SELECT'),
            'subscription_exists', s.oid IS NOT NULL, 'subscription_oid', s.oid,
            'forward_enabled', s.subenabled, 'disable_on_error', s.subdisableonerr,
            'table_sync_states', (SELECT json_object_agg(n.nspname || '.' || c.relname, r.srsubstate)
                                  FROM pg_subscription_rel AS r JOIN pg_class AS c ON c.oid = r.srrelid
                                  JOIN pg_namespace AS n ON n.oid = c.relnamespace WHERE r.srsubid = s.oid),
            'apply_workers', (SELECT count(a.pid) FROM pg_stat_subscription AS a
                              WHERE a.subid = s.oid AND a.worker_type = 'apply'),
            'worker_pid', w.pid, 'received_lsn', w.received_lsn::text, 'latest_end_lsn', w.latest_end_lsn::text,
            'target_apply_errors', e.apply_error_count, 'target_sync_errors', e.sync_error_count,
            'target_conflicts', e.confl_insert_exists + e.confl_update_origin_differs + e.confl_update_exists
                                + e.confl_update_missing + e.confl_delete_origin_differs + e.confl_delete_missing
                                + e.confl_multiple_unique_conflicts,
            'can_alter', pg_has_role(current_user, s.subowner, 'USAGE'),
            'can_call_control', EXISTS (
                SELECT FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'cutover' AND p.proname = 'set_subscription'
                  AND has_schema_privilege(current_user, n.oid, 'USAGE')
                  AND has_function_privilege(current_user, p.oid, 'EXECUTE')))
        FROM (SELECT 1) AS one
        LEFT JOIN pg_subscription AS s ON s.subname = %(forward)s AND s.subdbid = {CURRENT_DATABASE}
        LEFT JOIN pg_stat_subscription AS w ON w.subid = s.oid AND w.worker_type = 'apply'
        LEFT JOIN pg_stat_subscription_stats AS e ON e.subid = s.oid""",
    # Its own read: privileges are checked before execution, so a guard in the status read could not protect it.
    "target_origin": """
        SELECT remote_lsn::text FROM pg_replication_origin_status WHERE external_id = %(external_id)s""",
    "target_rows": """
        SELECT json_build_object(
            'messages', count(*), 'messages_max_id', max(id),
            'attachments', (SELECT count(*) FROM public.attachments))
        FROM public.messages""",
    # Bounded by the first source snapshot. The application only inserts, so these rows cannot change.
    # A digest per row, then a digest of those: a whole table must not sit in one value of a 1 GiB database.
    "business_tables": """
        SELECT json_build_object(
            'messages', (SELECT json_build_object(
                             'rows', count(*),
                             'sha256', encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(m::text, 'UTF8')), 'hex'), '' ORDER BY m.id), ''), 'UTF8')), 'hex'))
                         FROM public.messages AS m WHERE m.id <= %(messages_max_id)s),
            'attachments', (SELECT json_build_object(
                                'rows', count(*),
                                'sha256', encode(sha256(convert_to(coalesce(string_agg(encode(sha256(convert_to(a::text, 'UTF8')), 'hex'), '' ORDER BY a.id), ''), 'UTF8')), 'hex'))
                            FROM public.attachments AS a WHERE a.created_at <= %(attachments_max_created_at)s::timestamptz))""",
    "barrier_row": """
        SELECT json_build_object('barrier_id', barrier_id, 'epoch', epoch, 'created_at', created_at)
        FROM cutover.barrier WHERE barrier_id = %(barrier_id)s::uuid""",
    "platform_counts": """
        SELECT json_build_object(
            'holding', (SELECT count(*) FROM executor.executions WHERE status = ANY(%(statuses)s)),
            'executions', (SELECT count(*) FROM executor.executions),
            'leases', (SELECT count(*) FROM executor.leases),
            'resource_fences', (SELECT count(*) FROM executor.resource_fences))""",
    # Separate calls: has_table_privilege is true when any listed privilege is held, and cutover_control has
    # INSERT and SELECT. The DELETE's WHERE clause needs SELECT as well.
    "probe_status": f"""
        SELECT json_build_object(
            'system_identifier', (SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
            'database', pg_catalog.current_database(), 'in_recovery', pg_is_in_recovery(),
            'allowed', coalesce(has_table_privilege(current_user, {BARRIER_TABLE}, 'INSERT')
                                AND has_table_privilege(current_user, {BARRIER_TABLE}, 'DELETE')
                                AND has_table_privilege(current_user, {BARRIER_TABLE}, 'SELECT')
                                AND {BARRIER_SCHEMA_USABLE}, false))""",
}
NAMES = {"forward": FORWARD, "reverse": REVERSE, "publication": PUBLICATION}
# A failed one of these will not fix itself while waiting, so the post-check ends at once.
FATAL_CHECKS = ("forward_enabled", "source_slot_valid", "errors_not_increased", "single_writer_kept")
# Not an execution's epoch. The Executor finds its own barrier by id and never reads this column back.
PROBE_EPOCH = 0
PROBE_INSERT = "INSERT INTO cutover.barrier (barrier_id, epoch) VALUES (%s, %s)"
# The epoch keeps it off the Executor's rows, which always have one of 1 or more.
PROBE_DELETE = "DELETE FROM cutover.barrier WHERE barrier_id = %s AND epoch = %s"
PROBE_IDENTITY = "SELECT (SELECT system_identifier::text FROM pg_catalog.pg_control_system()), pg_catalog.current_database()"
MARKER_LOCK = "SELECT pg_catalog.pg_advisory_xact_lock(%s)"
MARKER_PROTOCOL = "serialized-v1"
ERROR_FIELDS = ("target_apply_errors", "target_sync_errors", "target_conflicts")
STAGE_SOURCE_FIELDS = ("source_observed_at", "reverse_enabled", "source_slot_active", "source_slot_wal_status",
                       "source_slot_invalidated", "source_sender_state", "source_sender_tls", "source_slot_confirmed_flush_lsn")
STAGE_TARGET_FIELDS = ("target_observed_at", "forward_enabled", "apply_workers", "target_origin_remote_lsn",
                       "target_app_login", "target_apply_errors", "target_sync_errors")
PLATFORM_FIELDS = ("executions", "leases", "resource_fences")
MIB = 1024 * 1024


def connect(dsn, environ=os.environ, *, options=READ_ONLY_OPTIONS, autocommit=False):
    """Refuses before libpq can send a password, unless the connection verifies the server (T8 #161)."""
    import psycopg  # Imported here so the rules and their tests need no driver.

    allow_loopback = environ.get(LOOPBACK_ENV) == "1"
    settings = tls_settings(psycopg.conninfo.conninfo_to_dict(dsn), environ, allow_loopback_plaintext=allow_loopback)
    connection = psycopg.connect(dsn, connect_timeout=10, application_name=APPLICATION_NAME, options=options,
                                 autocommit=autocommit, **settings)
    info = connection.info
    try:
        require_verified_tls(info.host, info.hostaddr, info.get_parameters().get("sslmode"),
                             allow_loopback_plaintext=allow_loopback)
    except AttachmentCopyFailed:
        connection.close()
        raise
    return connection


def read_one(dsn, name, params=None):
    """A session per read: none stays open between polls, and a dropped link needs no reconnect logic."""
    session = ReadOnlySession(connect(dsn), STATEMENTS)
    try:
        row = session.query(name, params)
        return row[0] if row else None
    finally:
        session.close()


def enable_method(state):
    if state.get("can_call_control"):
        return "control_function"
    return "alter_subscription" if state.get("can_alter") else None


def enable_statement(method):
    if method == "control_function":
        return f"SELECT cutover.set_subscription('{FORWARD}', true)"
    return runtime_state_sql("target", True)


def enable(dsn, method):
    with connect(dsn, options=CONTROL_OPTIONS, autocommit=True) as connection:
        connection.execute(enable_statement(method))


class ProbeTargetChanged(RuntimeError):
    pass


class MarkerWrite(NamedTuple):
    before_commit: str
    after_commit: str


def write_marker(dsn, statement, params, identity):
    """One committed statement, with positions before and after its acknowledged COMMIT.

    Checked in the session that writes, because the preflight's session has gone by then: the name in
    the DSN may lead elsewhere now, and verify-full proves only which server answered.
    """
    with connect(dsn, options=CONTROL_OPTIONS) as connection:
        if connection.execute(PROBE_IDENTITY).fetchone() != (identity["system_identifier"], identity["database"]):
            raise ProbeTargetChanged
        # The INSERT is submitted only after this lock is acknowledged. A later DELETE
        # must acquire the same lock, so a lost COMMIT response cannot race cleanup.
        # A collision only delays a different marker until lock_timeout; it cannot
        # change which UUID/epoch the fixed statements affect.
        key = int.from_bytes(hashlib.sha256(("bravo:t8:marker:v1:" + str(params[0])).encode()).digest()[:8], byteorder="big", signed=True)
        connection.execute(MARKER_LOCK, (key,))
        connection.execute(statement, params)
        before = connection.execute("SELECT pg_catalog.pg_current_wal_insert_lsn()::text").fetchone()[0]
        connection.commit()
        if connection.execute(PROBE_IDENTITY).fetchone() != (identity["system_identifier"], identity["database"]):
            raise ProbeTargetChanged
        after = connection.execute("SELECT pg_catalog.pg_current_wal_insert_lsn()::text").fetchone()[0]
        return MarkerWrite(before, after)


def describe(error):
    """A class name, or AttachmentCopyFailed's fixed reason. Driver messages can carry a host or DSN."""
    return error.reason if isinstance(error, AttachmentCopyFailed) else type(error).__name__


def read_target(target_dsn):
    target = read_one(target_dsn, "target_status", NAMES)
    target["target_origin_remote_lsn"] = None
    if target["origin_readable"] and target["subscription_exists"]:
        # A subscription's origin is named after its oid. This is the only position that means applied.
        target["target_origin_remote_lsn"] = read_one(target_dsn, "target_origin",
                                                      {"external_id": f"pg_{target['subscription_oid']}"})
    return target


def read_status(source_dsn, target_dsn):
    return {"target": read_target(target_dsn), "source": read_one(source_dsn, "source_status", NAMES)}


def read_platform(dsn):
    return read_one(dsn, "platform_counts", {"statuses": list(LEASE_HOLDING)})


def gather(source_dsn, target_dsn, executor_dsn, executor_not_deployed, probe_dsn=None):
    """Every read succeeds or leaves None behind: a state that cannot be seen is never a safe one."""
    state, errors = {}, {}

    def attempt(key, side, read):
        try:
            state[key] = read()
        except Exception as error:  # noqa: BLE001 - Any failure means unconfirmed; only its class is reported.
            state[key] = None
            errors.setdefault(side, describe(error))

    # Target first, as in the Executor's gate. Rows keep arriving, so the other order would show a healthy
    # target ahead of a source read a moment earlier.
    attempt("target", "target", lambda: read_target(target_dsn))
    attempt("target_rows", "target", lambda: read_one(target_dsn, "target_rows"))
    attempt("source", "source", lambda: read_one(source_dsn, "source_status", NAMES))
    attempt("source_rows", "source", lambda: read_one(source_dsn, "source_rows"))
    if executor_dsn:
        attempt("platform", "executor", lambda: read_platform(executor_dsn))
        platform = state.pop("platform")
        state["executor"] = ({"state": "unconfirmed"} if platform is None
                             else {"state": "holding" if platform["holding"] else "idle", **platform})
    else:
        state["executor"] = {"state": "declared_not_deployed" if executor_not_deployed else "unconfirmed"}
    if probe_dsn:
        attempt("probe", "probe", lambda: read_one(probe_dsn, "probe_status"))
    if errors:
        state["errors"] = errors
    return state


def source_refusals(source):
    found = []
    if source["in_recovery"]:
        found.append("source_is_not_primary")
    # None means the role could not be read, which is no more a safe state than False.
    if source["source_app_login"] is not True:
        found.append("source_app_cannot_login")
    if source["reverse_enabled"] or source["reverse_workers"]:
        found.append("reverse_active")
    if not source["slot_exists"]:
        return [*found, "slot_missing"]
    if source["slot_database"] != source["database"]:
        found.append("source_wrong_database")
    if source["slot_type"] != "logical":
        found.append("slot_not_logical")
    if source["source_slot_invalidated"]:
        found.append("slot_invalidated")
    elif source["source_slot_wal_status"] not in USABLE_WAL_STATUS:
        found.append("slot_at_risk")
    return found


def target_refusals(target):
    found = []
    if target["in_recovery"]:
        found.append("target_is_not_primary")
    if target["target_app_login"] is not False:
        found.append("target_app_can_login")
    if target["app_sessions"]:
        found.append("target_app_sessions")
    if target["reverse_slot_active"]:
        found.append("reverse_slot_in_use")
    if not target["subscription_exists"]:
        found.append("subscription_missing")
    elif not target["forward_enabled"] and enable_method(target) is None:
        found.append("no_enable_privilege")
    return found


def barrier_gaps(source, target):
    """Why a barrier row could not make the trip. schema.sql publishes business tables only; adding this
    table and refreshing the subscription is a per-environment step, so it is read here and never assumed."""
    gaps = []
    if not (source and source["barrier_published"]):
        gaps.append("barrier_not_published")
    if not (target and (target["table_sync_states"] or {}).get("cutover.barrier") == "r"):
        gaps.append("barrier_not_subscribed")
    return gaps


def probe_access_refusals(state):
    """The write has to land in the database verified as the source. verify-full proves only which server answered."""
    source, target, probe = state["source"], state["target"], state.get("probe")
    found = []
    if target and not target["barrier_readable"]:
        found.append("barrier_not_readable_on_target")
    if source and not source["barrier_readable"]:
        found.append("barrier_not_readable_on_source")
    if probe is None:
        return [*found, "barrier_probe_unconfirmed"]
    if source and (probe["system_identifier"], probe["database"]) != (source["system_identifier"], source["database"]):
        found.append("barrier_probe_wrong_database")
    if probe["in_recovery"]:
        found.append("barrier_probe_not_primary")
    if probe["allowed"] is not True:
        found.append("barrier_probe_not_permitted")
    return found


def refusals(state, *, probe=False):
    source, target = state["source"], state["target"]
    found = []
    found += ["source_unconfirmed"] if source is None or state["source_rows"] is None else source_refusals(source)
    found += ["target_unconfirmed"] if target is None or state["target_rows"] is None else target_refusals(target)
    if source and target and source["system_identifier"] == target["system_identifier"]:
        found.append("source_and_target_are_one_cluster")
    executor = state["executor"]["state"]
    if executor == "holding":
        found.append("executor_running")
    elif executor == "unconfirmed":
        found.append("executor_state_unconfirmed")
    if any(side and side["control_sessions"] for side in (source, target)):
        found.append("control_session_present")
    # A slot still held while the subscription is off has another reader, usually a walsender that has not
    # timed out yet. Enabling would only retry every 5 s (08P01) until it lets go, and if it is not an old
    # walsender, someone else is consuming this stream. Either way there is nothing to enable yet.
    if target and source and target["subscription_exists"] and not target["forward_enabled"] and source["source_slot_active"]:
        found.append("slot_in_use")
    if state["source_rows"] and state["target_rows"]:
        ahead = (state["target_rows"]["messages_max_id"] or 0) > (state["source_rows"]["messages_max_id"] or 0)
        if ahead or state["target_rows"]["attachments"] > state["source_rows"]["attachments"]:
            found.append("target_ahead_of_source")
    if probe:
        found += barrier_gaps(source, target) + probe_access_refusals(state)
    return found


def identity_refusals(args, state):
    """Nothing here can know which cluster is the right one, so the operator states it: --apply must
    carry the identifiers a dry run printed, after they were compared with the intended clusters."""
    found = []
    for side, pinned in (("source", args.source_system_id), ("target", args.target_system_id)):
        seen = state[side]["system_identifier"] if state[side] else None
        if pinned is None:
            if args.apply or args.cleanup_marker:
                found.append(f"{side}_identity_not_pinned")
        elif pinned != seen:
            found.append(f"{side}_identity_mismatch")
    return found


def baseline_of(state):
    source, target = state["source"], state["target"]
    return {"confirmed": parse_lsn(source["source_slot_confirmed_flush_lsn"]),
            "fixed": parse_lsn(source["source_wal_flush_lsn"]),
            "source_rows": state["source_rows"],
            "errors": {field: target[field] for field in ERROR_FIELDS}}


def rows_arrived(snapshot, rows):
    """Every row of the source snapshot. Later source writes may add rows but cannot remove these."""
    newest = snapshot["messages_max_id"]
    return (rows["messages"] >= snapshot["messages"] and rows["attachments"] >= snapshot["attachments"]
            and (newest is None or (rows["messages_max_id"] or 0) >= newest))


def single_writer_kept(source, target):
    return (target["target_app_login"] is False and not target["app_sessions"] and source["source_app_login"] is True
            and not source["reverse_enabled"] and not source["reverse_workers"] and not target["reverse_slot_active"])


def assess(baseline, state, *, rows_arrived, applied_floor=None):
    source, target = state["source"], state["target"]
    confirmed = parse_lsn(source["source_slot_confirmed_flush_lsn"])
    received = parse_lsn(target["received_lsn"])
    reported = parse_lsn(target["latest_end_lsn"])
    fixed = baseline["fixed"]
    behind = baseline["confirmed"] < fixed
    checks = {
        "forward_enabled": target["forward_enabled"] is True,
        "source_slot_valid": bool(source["slot_exists"]) and source["source_slot_invalidated"] is False,
        "apply_worker_running": bool(target["apply_workers"]),
        # The worker exists before it reaches the publisher. Only the slot shows that it did.
        "source_slot_active": source["source_slot_active"] is True,
        # With nothing behind, the slot has no reason to move, so the worker must report the position itself.
        "confirmed_flush_advanced": (confirmed is not None and confirmed > baseline["confirmed"]) if behind
        else (received is not None and received >= fixed),
        "received_fixed": received is not None and received >= fixed,
        # What the source knows was flushed, and what the Executor's lag gate will read.
        "confirmed_fixed": None not in (confirmed, reported) and min(confirmed, reported) >= fixed,
        "tables_ready": bool(target["table_sync_states"]) and set(target["table_sync_states"].values()) == {"r"},
        "rows_arrived": rows_arrived,
        "errors_not_increased": all(target[field] is not None and target[field] <= baseline["errors"][field]
                                    for field in ERROR_FIELDS),
        "single_writer_kept": single_writer_kept(source, target),
    }
    if applied_floor is not None:
        # The origin moves only on a commit. On a quiet source WAL alone leaves it behind for ever (PR #201 saw
        # it stay put until the test row), so it can be required only once such a commit has been made.
        applied = parse_lsn(target["target_origin_remote_lsn"])
        checks["applied_fixed"] = applied is not None and applied >= applied_floor
    return checks


def note_first(firsts, name, reached, now, previous, db_utc):
    if firsts is not None and reached and name not in firsts:
        firsts[name] = {"elapsed_s": round(now, 4), "previous_sample_s": None if previous is None else round(previous, 4),
                        "db_utc": db_utc}


def lsn_text(position):
    return f"{position >> 32:X}/{position & 0xFFFFFFFF:X}"


def post_check(baseline, read_state, read_rows, *, timeout, settle, poll, firsts=None, applied_floor=None,
               previous_sample=None, elapsed=None, clock=time.monotonic, sleep=time.sleep):
    """Pass only after every check holds for the whole settle period. Unconfirmed at the deadline is a failure."""
    started = clock()
    since_start = elapsed or (lambda: clock() - started)
    checks, read_errors, worker_pids, positions = {}, {}, set(), {}
    arrived, good_since, cleared_at, previous = False, None, None, previous_sample

    def result(passed, reason):
        return {"passed": passed, "reason": reason, "checks": checks, "seconds": round(clock() - started, 1),
                "confirmed_fixed_seconds": cleared_at, "positions": positions, "read_errors": read_errors,
                "worker_pids_seen": len(worker_pids), "last_sample_s": round(now, 4)}

    while True:
        now = since_start()
        try:
            state = read_state()
            source, target = state["source"], state["target"]
            if target["worker_pid"] is not None:
                worker_pids.add(target["worker_pid"])
            positions = {"fixed": lsn_text(baseline["fixed"]),
                         "source_slot_confirmed_flush_lsn": source["source_slot_confirmed_flush_lsn"],
                         "latest_end_lsn": target["latest_end_lsn"],
                         "target_origin_remote_lsn": target["target_origin_remote_lsn"]}
            checks = assess(baseline, state, rows_arrived=arrived, applied_floor=applied_floor)
            applied = parse_lsn(target["target_origin_remote_lsn"])
            db_utc = target["target_observed_at"]
            note_first(firsts, "worker", checks["apply_worker_running"], now, previous, db_utc)
            note_first(firsts, "received_fixed", checks["received_fixed"], now, previous, db_utc)
            note_first(firsts, "applied_fixed", applied is not None and applied >= baseline["fixed"], now, previous, db_utc)
            if checks["confirmed_fixed"]:
                if cleared_at is None:
                    cleared_at = round(clock() - started, 1)
                # Counting rows is the expensive read, and it can only pass once the position has.
                if not arrived:
                    arrived = rows_arrived(baseline["source_rows"], read_rows())
                    checks["rows_arrived"] = arrived
        except Exception as error:  # noqa: BLE001 - A read failure is unconfirmed; only its class is reported.
            name = describe(error)
            read_errors[name] = read_errors.get(name, 0) + 1
            good_since = None
        else:
            if not all(checks[name] for name in FATAL_CHECKS):
                return result(False, "failed_check")
            if all(checks.values()):
                good_since = clock() if good_since is None else good_since
                if clock() - good_since >= settle:
                    return result(True, "confirmed")
            else:
                good_since = None
        if clock() - started >= timeout:
            return result(False, "deadline")
        previous = now
        sleep(poll)


def wait_for(condition, *, elapsed, timeout, poll, sleep):
    """When the condition first held and the sample before it, or None. A failed read counts as not yet."""
    started, previous = elapsed(), None
    while True:
        now = elapsed()
        try:
            if condition():
                return {"elapsed_s": round(now, 4), "previous_sample_s": None if previous is None else round(previous, 4)}
        except Exception:  # noqa: BLE001, S110 - Nothing from it is reported; the deadline decides.
            pass
        if now - started >= timeout:
            return None
        previous = now
        sleep(poll)


def new_marker_report(barrier_id):
    """Submissions are what was sent. Commits are what is known: 1 from an answer or a read, None if neither came."""
    return {"barrier_id": barrier_id, "protocol": MARKER_PROTOCOL, "passed": False, "insert_lsn_before_commit": None, "delete_lsn_before_commit": None,
            "delete_lsn_after_commit": None,
            "same_row_on_both_sides_when_present": None, "source_insert_submissions": 0, "source_insert_commits": 0,
            "source_delete_submissions": 0, "source_delete_commits": 0, "barrier": None, "delete_arrived": None,
            "cleanup": "not_started"}


def note_error(report, error):
    # Its message can carry a host; class and state are enough.
    report["error_category"] = describe(error)
    report["sqlstate"] = getattr(error, "sqlstate", None)


def remove_marker(write, read_target_row, read_source_row, report, wait, *, read_target_applied=None):
    """Take one row off the source and confirm by reading on both sides. An answer alone proves neither."""
    barrier_id = report["barrier_id"]

    def remove():
        row = read_source_row()
        if report["source_insert_commits"] is None and row is not None:
            report["source_insert_commits"] = 1
        # Even a currently absent row can belong to an INSERT awaiting COMMIT.
        # DELETE takes the transaction lock before examining or changing that row.
        report["source_delete_submissions"] += 1
        try:
            written = write(PROBE_DELETE, (barrier_id, PROBE_EPOCH))
            report["delete_lsn_before_commit"] = written.before_commit
            report["delete_lsn_after_commit"] = written.after_commit
        except Exception as error:  # noqa: BLE001 - See note_error(). Removing by this id can safely repeat.
            note_error(report, error)
            return False
        report["source_delete_commits"] = 1
        return read_source_row() is None

    gone = wait(remove)

    def target_gone():
        if read_target_row() is not None:
            return False
        if report.get("barrier"):
            return True  # This run already observed the INSERT on the target.
        # Before INSERT was observed, target absence could simply mean that both
        # INSERT and DELETE are still in transit. Require actual apply progress.
        # A concurrent transaction could commit past the DELETE's pre-commit
        # position before this DELETE commits. Only the post-COMMIT position is a fence.
        floor = parse_lsn(report["delete_lsn_after_commit"])
        applied = parse_lsn(read_target_applied()) if read_target_applied else None
        return floor is not None and applied is not None and applied >= floor

    report["delete_arrived"] = wait(target_gone) if gone else None
    pending = [side for side, done in (("source", gone), ("target", report["delete_arrived"])) if not done]
    report["cleanup"] = "pending" if pending else "confirmed"
    if pending:
        report["pending_on"] = pending
        report["resume"] = (f"resume_replication.py --cleanup-marker {barrier_id} "
                            f"--marker-protocol {MARKER_PROTOCOL} --source-system-id <source> --target-system-id <target>")
    else:
        report.pop("pending_on", None)
        report.pop("resume", None)


def barrier_probe(write, read_target_row, read_source_row, barrier_id, *, on_delivered, on_cleaned, elapsed, timeout, poll,
                  sleep=time.sleep, read_target_applied=None):
    """One row through the whole path: a new write has to arrive, and its delete has to follow it.

    The insert is sent once. If its answer is lost the commit may still have happened, so only this run's id
    is read back and removed. Whatever goes wrong, the row does not knowingly stay on the source, and a
    cleanup that could not be confirmed is reported with the way to finish it. The target is only read.
    """
    report = new_marker_report(barrier_id)

    def wait(condition):
        return wait_for(condition, elapsed=elapsed, timeout=timeout, poll=poll, sleep=sleep)

    report["source_insert_submissions"] = 1
    try:
        report["insert_lsn_before_commit"] = write(PROBE_INSERT, (barrier_id, PROBE_EPOCH)).before_commit
        report["source_insert_commits"] = 1
    except Exception as error:  # noqa: BLE001 - See note_error().
        note_error(report, error)
        report["source_insert_commits"] = None
    if report["source_insert_commits"] == 1:
        try:
            report["barrier"] = wait(lambda: read_target_row() is not None)
            if report["barrier"]:
                report["same_row_on_both_sides_when_present"] = read_target_row() == read_source_row()
                on_delivered()
        except Exception as error:  # noqa: BLE001 - See note_error().
            note_error(report, error)
    remove_marker(write, read_target_row, read_source_row, report, wait, read_target_applied=read_target_applied)
    if report["barrier"] and report["delete_arrived"]:
        try:
            on_cleaned()
        except Exception as error:  # noqa: BLE001 - See note_error().
            note_error(report, error)
    report["passed"] = bool(report["barrier"] and report["delete_arrived"] and report["same_row_on_both_sides_when_present"]
                            and report["cleanup"] == "confirmed" and "error_category" not in report)
    return report


def stage_view(source, target):
    return {**{name: source[name] for name in STAGE_SOURCE_FIELDS}, **{name: target[name] for name in STAGE_TARGET_FIELDS}}


def before_enable_view(source):
    gap = parse_lsn(source["source_wal_flush_lsn"]) - parse_lsn(source["source_slot_confirmed_flush_lsn"])
    return {"observed_at": source["source_observed_at"], "source_wal_flush_lsn": source["source_wal_flush_lsn"],
            "source_slot_confirmed_flush_lsn": source["source_slot_confirmed_flush_lsn"], "gap_bytes": gap,
            "gap_mib": round(gap / MIB, 1), "slot_active": source["source_slot_active"], "wal_status": source["source_slot_wal_status"]}


def tables_view(source_before, source_now, target_now):
    """Rows and whether hashes match. The hashes themselves are not printed."""
    return {name: {"source_rows": source_now[name]["rows"], "target_rows": target_now[name]["rows"],
                   "sha256_equal": source_now[name]["sha256"] == target_now[name]["sha256"],
                   "source_sha256_unchanged": source_now[name]["sha256"] == source_before[name]["sha256"]}
            for name in ("messages", "attachments")}


class Run:
    def __init__(self, args, environ):
        self.args = args
        self.source_dsn, self.target_dsn = environ.get(SOURCE_DSN_ENV), environ.get(TARGET_DSN_ENV)
        self.executor_dsn, self.probe_dsn = environ.get(EXECUTOR_DSN_ENV), environ.get(PROBE_DSN_ENV)
        self.probe = args.apply and not args.no_barrier_probe
        mode = "cleanup-marker" if args.cleanup_marker else "apply" if args.apply else "dry-run"
        self.report = {"mode": mode, "passed": False, "stage": "preflight", "refusals": []}
        self.clock_start = None
        self.firsts = {}
        self.bounds = None
        self.source_tables_before = None

    def preflight(self):
        """Refusals, or the state to go on with. Run again right before ENABLE: hashing can take a while."""
        state = gather(self.source_dsn, self.target_dsn, self.executor_dsn, self.args.executor_not_deployed,
                       self.probe_dsn if self.probe else None)
        report = self.report
        if "errors" in state:
            report["errors"] = state.pop("errors")
        report["preflight"] = state
        report["refusals"] = refusals(state, probe=self.probe) + identity_refusals(self.args, state)
        return state

    def elapsed(self):
        return time.monotonic() - self.clock_start

    def tables(self, dsn):
        return read_one(dsn, "business_tables", self.bounds)

    def stage(self, name):
        status = read_status(self.source_dsn, self.target_dsn)
        source_now, target_now = self.tables(self.source_dsn), self.tables(self.target_dsn)
        if self.source_tables_before is None:
            self.source_tables_before = source_now
        view = stage_view(status["source"], status["target"])
        view["business_tables"] = tables_view(self.source_tables_before, source_now, target_now)
        self.report.setdefault("stages", {})[name] = view
        return status, view

    def tables_match(self):
        """Read last, and more than once: a row committed between the two reads differs only for a moment."""
        def equal():
            source_now, target_now = self.tables(self.source_dsn), self.tables(self.target_dsn)
            return all(table["sha256_equal"] for table in tables_view(self.source_tables_before, source_now, target_now).values())

        return wait_for(equal, elapsed=self.elapsed, timeout=self.args.probe_timeout, poll=self.args.probe_poll, sleep=time.sleep) is not None

    def marker_rows(self, barrier_id, dsn):
        return read_one(dsn, "barrier_row", {"barrier_id": barrier_id})

    def writer(self, source):
        identity = {"system_identifier": source["system_identifier"], "database": source["database"]}
        return lambda statement, params: write_marker(self.probe_dsn, statement, params, identity)

    def clean_up(self):
        """Finish the cleanup a failed run left pending. Only the row named here, only a marker, only on the source."""
        args, report = self.args, self.report
        barrier_id = args.cleanup_marker
        required = [(SOURCE_DSN_ENV, self.source_dsn), (TARGET_DSN_ENV, self.target_dsn), (PROBE_DSN_ENV, self.probe_dsn)]
        report["refusals"] = [f"missing_{name}" for name, value in required if not value]
        try:
            uuid.UUID(barrier_id)
        except ValueError:
            report["refusals"] = ["invalid_barrier_id"]
        if report["refusals"]:
            return
        if args.marker_protocol != MARKER_PROTOCOL:
            report["refusals"] = ["marker_protocol_unconfirmed"]
            return
        state = gather(self.source_dsn, self.target_dsn, None, True, self.probe_dsn)
        state.pop("executor")  # Not read here: removing a marker by its id touches nothing an execution uses.
        if "errors" in state:
            report["errors"] = state.pop("errors")
        report["preflight"] = state
        source, target = state["source"], state["target"]
        found = identity_refusals(args, state) + probe_access_refusals(state)
        found += ["source_unconfirmed"] if source is None else []
        found += ["target_unconfirmed"] if target is None else []
        if source and source["slot_exists"] and source["slot_database"] != source["database"]:
            found.append("source_wrong_database")
        report["refusals"] = found
        if found:
            return

        def source_row():
            return self.marker_rows(barrier_id, self.source_dsn)

        row = source_row()
        if row is not None and row["epoch"] != PROBE_EPOCH:
            report["refusals"] = ["not_a_marker_row"]
            return
        report["stage"] = "cleanup"
        self.clock_start = time.monotonic()
        marker = report["marker"] = new_marker_report(barrier_id)
        marker["source_insert_commits"] = None

        def wait(condition):
            return wait_for(condition, elapsed=self.elapsed, timeout=args.probe_timeout, poll=args.probe_poll, sleep=time.sleep)

        remove_marker(self.writer(source), lambda: self.marker_rows(barrier_id, self.target_dsn), source_row, marker, wait,
                      read_target_applied=lambda: read_target(self.target_dsn)["target_origin_remote_lsn"])
        report["passed"] = marker["passed"] = marker["cleanup"] == "confirmed"

    def send_marker(self, source):
        barrier_id = str(uuid.uuid4())
        report = self.report
        marker = barrier_probe(self.writer(source),
                               lambda: self.marker_rows(barrier_id, self.target_dsn),
                               lambda: self.marker_rows(barrier_id, self.source_dsn), barrier_id,
                               on_delivered=lambda: self.stage("delivered"), on_cleaned=lambda: self.stage("cleaned"),
                               elapsed=self.elapsed, timeout=self.args.probe_timeout, poll=self.args.probe_poll,
                               read_target_applied=lambda: read_target(self.target_dsn)["target_origin_remote_lsn"])
        report["marker"] = marker
        if marker.get("barrier"):
            self.firsts["barrier"] = {**marker["barrier"], "db_utc": report["stages"].get("delivered", {}).get("target_observed_at")}
        return marker

    def finish_continuity(self, state):
        report = self.report
        status = read_status(self.source_dsn, self.target_dsn)
        continuity = report["continuity"]
        # A subscription's origin is named after its oid, so the same oid means the same origin.
        continuity["same_subscription_oid_and_origin"] = status["target"]["subscription_oid"] == state["target"]["subscription_oid"]
        continuity["table_sync_states"] = status["target"]["table_sync_states"]
        unchanged = continuity["same_subscription_oid_and_origin"]
        if self.executor_dsn:
            after = read_platform(self.executor_dsn)
            platform_before = state["executor"]
            continuity["platform_before_after"] = {name: [platform_before[name], after[name]] for name in PLATFORM_FIELDS}
            # A finished execution is no danger, but one that started meanwhile raced this whole run.
            unchanged = unchanged and not after["holding"]
        return unchanged

    def execute(self):
        args, report = self.args, self.report
        required = [(SOURCE_DSN_ENV, self.source_dsn), (TARGET_DSN_ENV, self.target_dsn),
                    *([(PROBE_DSN_ENV, self.probe_dsn)] if self.probe else [])]
        missing = [f"missing_{name}" for name, value in required if not value]
        if missing:
            report["refusals"] = missing
            return
        state = self.preflight()
        if report["refusals"]:
            return
        report["barrier_gaps"] = barrier_gaps(state["source"], state["target"])
        self.bounds = {key: state["source_rows"][key] for key in ("messages_max_id", "attachments_max_created_at")}
        self.stage("before")
        # Reads of the state just before ENABLE, because hashing may have taken a while.
        state = self.preflight()
        if report["refusals"]:
            return
        target = state["target"]
        report["immediately_before_enable"] = before_enable_view(state["source"])
        report["continuity"] = {"enable_submissions": 0, "source_write_block_before": args.source_write_block,
                                "source_wal_sender_timeout_ms": state["source"]["source_wal_sender_timeout_ms"],
                                "target_wal_receiver_timeout_ms": target["target_wal_receiver_timeout_ms"],
                                "disable_on_error": target["disable_on_error"]}
        method = None if target["forward_enabled"] else enable_method(target)
        if not args.apply:
            report["would_run"] = None if method is None else enable_statement(method)
            report["marker"] = {"state": "not_run_without_apply"}
            if method is not None:
                report["passed"] = True
                return
        report["enabled_by"] = method
        self.clock_start = time.monotonic()
        clock = report["command_clock"] = {"start": "this command's monotonic clock, immediately before ENABLE (or before the "
                                                    "first read, if the subscription was already on)",
                                           "enable_command_return_s": None, "first_observations": self.firsts}
        if method is not None:
            report["stage"] = "enable"
            # Before the call: the server may have committed it even if the connection then fails.
            report["continuity"]["enable_submissions"] = 1
            try:
                enable(self.target_dsn, method)
            except Exception as error:  # noqa: BLE001 - Its message can carry a host; class and state are enough.
                report["error_category"] = describe(error)
                report["sqlstate"] = getattr(error, "sqlstate", None)
                return
            clock["enable_command_return_s"] = round(self.elapsed(), 4)
        baseline = baseline_of(state)

        def status():
            return read_status(self.source_dsn, self.target_dsn)

        def rows():
            return read_one(self.target_dsn, "target_rows")

        common = {"firsts": self.firsts, "elapsed": self.elapsed, "poll": args.poll}
        report["stage"] = "catch_up"
        report["catch_up"] = post_check(baseline, status, rows, timeout=args.timeout, settle=0, **common)
        if not report["catch_up"]["passed"]:
            return
        applied_floor = None
        if self.probe:
            report["stage"] = "marker"
            marker = self.send_marker(state["source"])
            if not marker["passed"]:
                return
            # Its commit is past the fixed position, so the applied position must now have passed both. Reading
            # it takes more than pg_read_all_stats. Where the role cannot, the delete's arrival proves the same.
            readable = target["origin_readable"]
            report["continuity"]["applied_position"] = "read_from_origin" if readable else "implied_by_marker"
            applied_floor = parse_lsn(marker["delete_lsn_before_commit"]) if readable else None
        else:
            report["marker"] = report.get("marker", {"state": "skipped_by_operator"})
        report["stage"] = "post_check"
        report["post_check"] = post_check(baseline, status, rows, timeout=args.timeout, settle=args.settle, applied_floor=applied_floor,
                                          previous_sample=report["catch_up"]["last_sample_s"], **common)
        if not report["post_check"]["passed"]:
            return
        if not self.probe:
            self.stage("final")
        report["stage"] = "continuity"
        unchanged = self.finish_continuity(state)
        report["continuity"]["business_tables_match"] = self.tables_match()
        report["passed"] = unchanged and report["continuity"]["business_tables_match"]


def run(args, environ):
    work = Run(args, environ)
    try:
        work.clean_up() if args.cleanup_marker else work.execute()
    except Exception as error:  # noqa: BLE001 - A traceback could print a driver message, so never let one out.
        # Added to the report, not in place of it: it may be what says that ENABLE was already sent.
        work.report.update(passed=False, error_category=describe(error))
    return work.report


def main(argv, environ):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true",
                        help="enable the forward subscription if it is off, and send one barrier row through it")
    action.add_argument("--cleanup-marker", metavar="BARRIER_ID",
                        help="finish a cleanup a failed run reported as pending: remove only this test row from the source")
    parser.add_argument("--marker-protocol", choices=[MARKER_PROTOCOL],
                        help="required with --cleanup-marker: copy the protocol from the original marker report; legacy runs need manual settlement")
    parser.add_argument("--source-system-id", help="required to write: the source identifier printed by a dry run")
    parser.add_argument("--target-system-id", help="required to write: the target identifier printed by a dry run")
    parser.add_argument("--source-write-block", choices=["on", "off"], default="not_read",
                        help="what the operator read from the application beforehand. Only recorded: never read or changed here")
    parser.add_argument("--no-barrier-probe", action="store_true",
                        help="operator's choice, recorded in the output, where the barrier table is not replicated yet")
    parser.add_argument("--executor-not-deployed", action="store_true",
                        help=f"operator's statement, recorded in the output, for when {EXECUTOR_DSN_ENV} cannot exist")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for the post-check")
    parser.add_argument("--settle", type=float, default=15, help="seconds every check must keep holding")
    parser.add_argument("--poll", type=float, default=1, help="seconds between post-check reads")
    parser.add_argument("--probe-timeout", type=float, default=60, help="seconds to wait for each barrier arrival")
    parser.add_argument("--probe-poll", type=float, default=0.2, help="seconds between barrier reads")
    args = parser.parse_args(argv)
    report = run(args, environ)
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
