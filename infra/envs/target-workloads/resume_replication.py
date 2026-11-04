#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.6"]
# ///
"""Resume only the forward subscription after it turned itself off, then prove it caught up.

The default writes nothing: read-only checks, and if the subscription is already on, the read-only
post-check as well. --apply adds at most two things, and only when every check passed: one ENABLE
of the forward subscription if it is off, and one test row in cutover.barrier that is deleted again.
A running subscription is never enabled a second time. Slots, roles and the reverse subscription
are never touched, and nothing is ever written to the target.

Connections come only from the environment. Output is one JSON document without DSNs, hosts or
driver messages. Exit 0 means the checks passed; anything not confirmed exits 1.
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().with_name("testing")))

from attachment_copy import AttachmentCopyFailed, require_verified_tls, tls_settings
from lag_metrics import parse_lsn
from lag_readonly import NotReadOnly, ReadOnlySession  # noqa: F401 - Part of what callers catch.
from replication import names, runtime_state_sql, subscription

SOURCE_DSN_ENV = "T8_RESUME_SOURCE_DSN"
TARGET_DSN_ENV = "T8_RESUME_TARGET_DSN"
EXECUTOR_DSN_ENV = "T8_RESUME_EXECUTOR_DSN"
# Its own variable, so that the read DSN can never write: needs INSERT and DELETE on cutover.barrier.
PROBE_DSN_ENV = "T8_RESUME_SOURCE_PROBE_DSN"
# Local test databases only. Applies when host and hostaddr are both literal loopback addresses.
LOOPBACK_ENV = "T8_RESUME_ALLOW_LOOPBACK_PLAINTEXT"
FORWARD = names("target")[2]
REVERSE = names("source")[2]
PUBLICATION = subscription("target")["spec"]["publicationName"]
APPLICATION_NAME = "t8_resume"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000"
CONTROL_OPTIONS = "-c statement_timeout=10000 -c lock_timeout=5000"
# bravo_cutover.contract.LEASE_HOLDING: an execution in one of these still owns the databases.
LEASE_HOLDING = ("PENDING", "RUNNING", "SAFE_HOLD")
USABLE_WAL_STATUS = ("reserved", "extended")
CURRENT_DATABASE = "(SELECT oid FROM pg_database WHERE datname = current_database())"
# Catalog joins, not to_regclass: naming a schema the role has no USAGE on raises instead of returning NULL.
BARRIER_TABLE = """(SELECT c.oid FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'cutover' AND c.relname = 'barrier')"""
STATEMENTS = {
    "source_status": f"""
        SELECT json_build_object(
            'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
            'in_recovery', pg_is_in_recovery(),
            'wal_lsn', CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_insert_lsn()::text END,
            'barrier_published', EXISTS (SELECT FROM pg_publication_tables WHERE pubname = %(publication)s
                                         AND schemaname = 'cutover' AND tablename = 'barrier'),
            'app_can_login', (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'),
            'control_sessions', (SELECT count(*) FROM pg_stat_activity
                                 WHERE usename = 'cutover_control' AND pid <> pg_backend_pid()),
            'wal_sender_timeout', current_setting('wal_sender_timeout'),
            'max_slot_wal_keep_size', current_setting('max_slot_wal_keep_size'),
            'slot', (SELECT json_build_object(
                         'type', slot_type, 'active', active, 'wal_status', wal_status,
                         'safe_wal_size', safe_wal_size, 'invalidation_reason', invalidation_reason,
                         'confirmed_flush_lsn', confirmed_flush_lsn::text)
                     FROM pg_replication_slots WHERE slot_name = %(forward)s),
            'reverse', (SELECT json_build_object(
                            'enabled', s.subenabled,
                            'workers', (SELECT count(w.pid) FROM pg_stat_subscription AS w WHERE w.subid = s.oid))
                        FROM pg_subscription AS s
                        WHERE s.subname = %(reverse)s AND s.subdbid = {CURRENT_DATABASE}))""",
    # One snapshot: every row counted here committed before the position read in the same statement.
    "source_rows": """
        SELECT json_build_object(
            'messages', count(*), 'messages_max_id', max(id),
            'attachments', (SELECT count(*) FROM public.attachments),
            'wal_lsn', pg_current_wal_insert_lsn()::text)
        FROM public.messages""",
    "target_status": f"""
        SELECT json_build_object(
            'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
            'in_recovery', pg_is_in_recovery(),
            'app_can_login', (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'),
            'app_sessions', (SELECT count(*) FROM pg_stat_activity
                             WHERE usename = 'chat_app' AND pid <> pg_backend_pid()),
            'control_sessions', (SELECT count(*) FROM pg_stat_activity
                                 WHERE usename = 'cutover_control' AND pid <> pg_backend_pid()),
            'wal_receiver_timeout', current_setting('wal_receiver_timeout'),
            'reverse_slot_active', (SELECT active FROM pg_replication_slots WHERE slot_name = %(reverse)s),
            'subscription', (SELECT json_build_object(
                    'enabled', s.subenabled, 'disable_on_error', s.subdisableonerr,
                    'tables_not_ready', (SELECT count(*) FROM pg_subscription_rel AS r
                                         WHERE r.srsubid = s.oid AND r.srsubstate <> 'r'),
                    'barrier_subscribed', EXISTS (SELECT FROM pg_subscription_rel AS r
                                                  WHERE r.srsubid = s.oid AND r.srsubstate = 'r'
                                                    AND r.srrelid = {BARRIER_TABLE}),
                    'barrier_readable', coalesce(
                        has_table_privilege(current_user, {BARRIER_TABLE}, 'SELECT')
                        AND EXISTS (SELECT FROM pg_namespace WHERE nspname = 'cutover'
                                    AND has_schema_privilege(current_user, oid, 'USAGE')), false),
                    'worker_pid', w.pid, 'received_lsn', w.received_lsn::text,
                    'latest_end_lsn', w.latest_end_lsn::text,
                    'apply_error_count', e.apply_error_count, 'sync_error_count', e.sync_error_count,
                    'conflicts', e.confl_insert_exists + e.confl_update_origin_differs + e.confl_update_exists
                                 + e.confl_update_missing + e.confl_delete_origin_differs + e.confl_delete_missing
                                 + e.confl_multiple_unique_conflicts,
                    'can_alter', pg_has_role(current_user, s.subowner, 'USAGE'),
                    'can_call_control', EXISTS (
                        SELECT FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
                        WHERE n.nspname = 'cutover' AND p.proname = 'set_subscription'
                          AND has_schema_privilege(current_user, n.oid, 'USAGE')
                          AND has_function_privilege(current_user, p.oid, 'EXECUTE')))
                FROM pg_subscription AS s
                LEFT JOIN pg_stat_subscription AS w ON w.subid = s.oid AND w.worker_type = 'apply'
                LEFT JOIN pg_stat_subscription_stats AS e ON e.subid = s.oid
                WHERE s.subname = %(forward)s AND s.subdbid = {CURRENT_DATABASE}))""",
    "target_rows": """
        SELECT json_build_object(
            'messages', count(*), 'messages_max_id', max(id),
            'attachments', (SELECT count(*) FROM public.attachments))
        FROM public.messages""",
    "executor_holders": "SELECT count(*) FROM executor.executions WHERE status = ANY(%(statuses)s)",
    # Separate calls: has_table_privilege is true when any listed privilege is held, and cutover_control has
    # INSERT and SELECT. The DELETE's WHERE clause needs SELECT as well.
    "probe_allowed": f"""
        SELECT coalesce(has_table_privilege(current_user, {BARRIER_TABLE}, 'INSERT')
                        AND has_table_privilege(current_user, {BARRIER_TABLE}, 'DELETE')
                        AND has_table_privilege(current_user, {BARRIER_TABLE}, 'SELECT')
                        AND EXISTS (SELECT FROM pg_namespace WHERE nspname = 'cutover'
                                    AND has_schema_privilege(current_user, oid, 'USAGE')), false)""",
    "barrier_rows": "SELECT count(*) FROM cutover.barrier WHERE barrier_id = %(barrier_id)s::uuid",
}
NAMES = {"forward": FORWARD, "reverse": REVERSE, "publication": PUBLICATION}
# A failed one of these will not fix itself while waiting, so the post-check ends at once.
FATAL_CHECKS = ("subscription_enabled", "slot_valid", "errors_not_increased", "single_writer_kept")
# Not an execution's epoch. The Executor finds its own barrier by id and never reads this column back.
PROBE_EPOCH = 0
PROBE_INSERT = "INSERT INTO cutover.barrier (barrier_id, epoch) VALUES (%s, %s)"
PROBE_DELETE = "DELETE FROM cutover.barrier WHERE barrier_id = %s"
ERROR_FIELDS = ("apply_error_count", "sync_error_count", "conflicts")


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
        return session.query(name, params)[0]
    finally:
        session.close()


def enable_method(subscription):
    if subscription.get("can_call_control"):
        return "control_function"
    return "alter_subscription" if subscription.get("can_alter") else None


def enable_statement(method):
    if method == "control_function":
        return f"SELECT cutover.set_subscription('{FORWARD}', true)"
    return runtime_state_sql("target", True)


def enable(dsn, method):
    with connect(dsn, options=CONTROL_OPTIONS, autocommit=True) as connection:
        connection.execute(enable_statement(method))


def describe(error):
    """A class name, or AttachmentCopyFailed's fixed reason. Driver messages can carry a host or DSN."""
    return error.reason if isinstance(error, AttachmentCopyFailed) else type(error).__name__


def read_status(source_dsn, target_dsn):
    return {"source": read_one(source_dsn, "source_status", NAMES),
            "target": read_one(target_dsn, "target_status", NAMES)}


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
    attempt("target", "target", lambda: read_one(target_dsn, "target_status", NAMES))
    attempt("target_rows", "target", lambda: read_one(target_dsn, "target_rows"))
    attempt("source", "source", lambda: read_one(source_dsn, "source_status", NAMES))
    attempt("source_rows", "source", lambda: read_one(source_dsn, "source_rows"))
    if executor_dsn:
        attempt("holders", "executor", lambda: read_one(executor_dsn, "executor_holders",
                                                        {"statuses": list(LEASE_HOLDING)}))
        holders = state.pop("holders")
        state["executor"] = ({"state": "unconfirmed"} if holders is None
                             else {"state": "holding" if holders else "idle", "holders": holders})
    else:
        state["executor"] = {"state": "declared_not_deployed" if executor_not_deployed else "unconfirmed"}
    if probe_dsn:
        attempt("probe_allowed", "probe", lambda: read_one(probe_dsn, "probe_allowed"))
    if errors:
        state["errors"] = errors
    return state


def source_refusals(source):
    found = []
    if source["in_recovery"]:
        found.append("source_is_not_primary")
    # None means the role could not be read, which is no more a safe state than False.
    if source["app_can_login"] is not True:
        found.append("source_app_cannot_login")
    reverse = source["reverse"]
    if reverse and (reverse["enabled"] or reverse["workers"]):
        found.append("reverse_active")
    slot = source["slot"]
    if slot is None:
        return [*found, "slot_missing"]
    if slot["type"] != "logical":
        found.append("slot_not_logical")
    if slot["invalidation_reason"] is not None or slot["wal_status"] == "lost":
        found.append("slot_invalidated")
    elif slot["wal_status"] not in USABLE_WAL_STATUS:
        found.append("slot_at_risk")
    return found


def target_refusals(target):
    found = []
    if target["in_recovery"]:
        found.append("target_is_not_primary")
    if target["app_can_login"] is not False:
        found.append("target_app_can_login")
    if target["app_sessions"]:
        found.append("target_app_sessions")
    if target["reverse_slot_active"]:
        found.append("reverse_slot_in_use")
    subscription = target["subscription"]
    if subscription is None:
        found.append("subscription_missing")
    elif not subscription["enabled"] and enable_method(subscription) is None:
        found.append("no_enable_privilege")
    return found


def barrier_gaps(source, target):
    """Why a barrier row could not make the trip. schema.sql publishes business tables only; adding this
    table and refreshing the subscription is a per-environment step, so it is read here and never assumed."""
    subscription = (target and target["subscription"]) or {}
    gaps = []
    if not (source and source["barrier_published"]):
        gaps.append("barrier_not_published")
    if not subscription.get("barrier_subscribed"):
        gaps.append("barrier_not_subscribed")
    return gaps


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
    subscription = target and target["subscription"]
    slot = source and source["slot"]
    # A slot still held while the subscription is off has another reader, usually a walsender that has
    # not timed out yet. Enabling now fails with 55006, and disable_on_error switches it off again.
    if subscription and slot and not subscription["enabled"] and slot["active"]:
        found.append("slot_in_use")
    if state["source_rows"] and state["target_rows"]:
        ahead = (state["target_rows"]["messages_max_id"] or 0) > (state["source_rows"]["messages_max_id"] or 0)
        if ahead or state["target_rows"]["attachments"] > state["source_rows"]["attachments"]:
            found.append("target_ahead_of_source")
    if probe:
        found += barrier_gaps(source, target)
        if subscription and not subscription["barrier_readable"]:
            found.append("barrier_not_readable_on_target")
        if state.get("probe_allowed") is not True:
            found.append("barrier_probe_not_permitted")
    return found


def identity_refusals(args, state):
    """Nothing here can know which cluster is the right one, so the operator states it: --apply must
    carry the identifiers a dry run printed, after they were compared with the intended clusters."""
    found = []
    for side, pinned in (("source", args.source_system_id), ("target", args.target_system_id)):
        seen = state[side]["system_identifier"] if state[side] else None
        if pinned is None:
            if args.apply:
                found.append(f"{side}_identity_not_pinned")
        elif pinned != seen:
            found.append(f"{side}_identity_mismatch")
    return found


def error_counts(subscription):
    return {field: subscription[field] for field in ERROR_FIELDS}


def baseline_of(state):
    return {"confirmed": parse_lsn(state["source"]["slot"]["confirmed_flush_lsn"]),
            "watermark": parse_lsn(state["source_rows"]["wal_lsn"]),
            "source_rows": state["source_rows"],
            "errors": error_counts(state["target"]["subscription"])}


def rows_arrived(watermark, rows):
    """Every row of the watermark snapshot. Later source writes may add rows but cannot remove these."""
    newest = watermark["messages_max_id"]
    return (rows["messages"] >= watermark["messages"] and rows["attachments"] >= watermark["attachments"]
            and (newest is None or (rows["messages_max_id"] or 0) >= newest))


def single_writer_kept(source, target):
    reverse = source["reverse"]
    return (target["app_can_login"] is False and not target["app_sessions"] and source["app_can_login"] is True
            and not (reverse and (reverse["enabled"] or reverse["workers"])) and not target["reverse_slot_active"])


def assess(baseline, state, *, rows_arrived):
    source, target = state["source"], state["target"]
    subscription, slot = target["subscription"], source["slot"]
    confirmed = parse_lsn(slot["confirmed_flush_lsn"]) if slot else None
    received = parse_lsn(subscription["received_lsn"]) if subscription else None
    applied = parse_lsn(subscription["latest_end_lsn"]) if subscription else None
    watermark = baseline["watermark"]
    behind = baseline["confirmed"] < watermark
    return {
        "subscription_enabled": bool(subscription and subscription["enabled"]),
        "slot_valid": bool(slot and slot["invalidation_reason"] is None and slot["wal_status"] != "lost"),
        "worker_running": bool(subscription and subscription["worker_pid"] is not None),
        # The worker exists before it reaches the publisher. Only the slot shows that it did.
        "publisher_connected": bool(slot and slot["active"]),
        # With nothing behind, the slot has no reason to move, so the worker must report the watermark itself.
        "lsn_advanced": (confirmed is not None and confirmed > baseline["confirmed"]) if behind
        else (received is not None and received >= watermark),
        # Both views: what the source knows was flushed, and what the Executor's lag gate will read.
        "backlog_cleared": None not in (confirmed, applied) and min(confirmed, applied) >= watermark,
        "tables_ready": bool(subscription and subscription["tables_not_ready"] == 0),
        "rows_arrived": rows_arrived,
        "errors_not_increased": bool(subscription) and all(
            subscription[field] <= baseline["errors"][field] for field in ERROR_FIELDS),
        "single_writer_kept": single_writer_kept(source, target),
    }


def post_check(baseline, read_state, read_rows, *, timeout, settle, poll, clock=time.monotonic, sleep=time.sleep):
    """Pass only after every check holds for the whole settle period. Unconfirmed at the deadline is a failure."""
    started = clock()
    checks, read_errors, worker_pids, positions = {}, {}, set(), {}
    arrived, good_since, cleared_at = False, None, None

    def result(passed, reason):
        return {"passed": passed, "reason": reason, "checks": checks, "seconds": round(clock() - started, 1),
                "backlog_cleared_seconds": cleared_at, "positions": positions, "read_errors": read_errors,
                "worker_pids_seen": len(worker_pids)}

    while True:
        try:
            state = read_state()
            subscription, slot = state["target"]["subscription"], state["source"]["slot"]
            if subscription and subscription["worker_pid"] is not None:
                worker_pids.add(subscription["worker_pid"])
            positions = {"watermark": lsn_text(baseline["watermark"]),
                         "confirmed_flush": slot and slot["confirmed_flush_lsn"],
                         "latest_end": subscription and subscription["latest_end_lsn"]}
            checks = assess(baseline, state, rows_arrived=arrived)
            if checks["backlog_cleared"]:
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
        sleep(poll)


def lsn_text(position):
    return f"{position >> 32:X}/{position & 0xFFFFFFFF:X}"


def wait_for_rows(read_rows, wanted, *, timeout, poll, clock, sleep):
    """Milliseconds until the row count is the wanted one, or None. A failed read just counts as not yet."""
    started = clock()
    while True:
        try:
            if read_rows() == wanted:
                return round((clock() - started) * 1000)
        except Exception:  # noqa: BLE001, S110 - Nothing from it is reported; the deadline decides.
            pass
        if clock() - started >= timeout:
            return None
        sleep(poll)


def barrier_probe(write, read_rows, barrier_id, *, timeout, poll, clock=time.monotonic, sleep=time.sleep):
    """One row through the whole path: a new write has to arrive, and its delete has to follow it.

    The delete runs even if the row never arrived, so a failed probe leaves nothing on the source. The
    target is only read: a row it received and never lost again is reported, not removed.
    """
    report = {"barrier_id": barrier_id, "passed": False, "inserted": False, "arrived_ms": None,
              "deleted_on_source": False, "delete_arrived_ms": None}
    try:
        write(PROBE_INSERT, (barrier_id, PROBE_EPOCH))
        report["inserted"] = True
        report["arrived_ms"] = wait_for_rows(read_rows, 1, timeout=timeout, poll=poll, clock=clock, sleep=sleep)
        write(PROBE_DELETE, (barrier_id,))
        report["deleted_on_source"] = True
        if report["arrived_ms"] is not None:
            report["delete_arrived_ms"] = wait_for_rows(read_rows, 0, timeout=timeout, poll=poll, clock=clock, sleep=sleep)
    except Exception as error:  # noqa: BLE001 - Its message can carry a host; class and state are enough.
        report["error_category"] = describe(error)
        report["sqlstate"] = getattr(error, "sqlstate", None)
    report["passed"] = None not in (report["arrived_ms"], report["delete_arrived_ms"])
    return report


def write_probe(dsn):
    def write(statement, params):
        with connect(dsn, options=CONTROL_OPTIONS, autocommit=True) as connection:
            connection.execute(statement, params)

    return write


def run(args, environ):
    source_dsn, target_dsn = environ.get(SOURCE_DSN_ENV), environ.get(TARGET_DSN_ENV)
    probe_dsn = environ.get(PROBE_DSN_ENV)
    probe = args.apply and not args.no_barrier_probe
    report = {"mode": "apply" if args.apply else "dry-run", "passed": False, "stage": "preflight", "refusals": []}
    required = [(SOURCE_DSN_ENV, source_dsn), (TARGET_DSN_ENV, target_dsn), *([(PROBE_DSN_ENV, probe_dsn)] if probe else [])]
    missing = [f"missing_{name}" for name, value in required if not value]
    if missing:
        report["refusals"] = missing
        return report
    state = gather(source_dsn, target_dsn, environ.get(EXECUTOR_DSN_ENV), args.executor_not_deployed,
                   probe_dsn if probe else None)
    if "errors" in state:
        report["errors"] = state.pop("errors")
    report["preflight"] = state
    report["refusals"] = refusals(state, probe=probe) + identity_refusals(args, state)
    if report["refusals"]:
        return report
    report["barrier_gaps"] = barrier_gaps(state["source"], state["target"])
    report["backlog_bytes"] = parse_lsn(state["source_rows"]["wal_lsn"]) - parse_lsn(state["source"]["slot"]["confirmed_flush_lsn"])
    subscription = state["target"]["subscription"]
    method = None if subscription["enabled"] else enable_method(subscription)
    if not args.apply:
        report["would_run"] = None if method is None else enable_statement(method)
        if method is not None:
            report["passed"] = True
            return report
    else:
        report["stage"] = "enable"
        report["enabled_by"] = method
        if method is not None:
            try:
                enable(target_dsn, method)
            except Exception as error:  # noqa: BLE001 - Its message can carry a host; class and state are enough.
                report["error_category"] = describe(error)
                report["sqlstate"] = getattr(error, "sqlstate", None)
                return report
    report["stage"] = "post_check"
    baseline = baseline_of(state)

    def status():
        return read_status(source_dsn, target_dsn)

    def rows():
        return read_one(target_dsn, "target_rows")

    report["post_check"] = post_check(baseline, status, rows, timeout=args.timeout, settle=args.settle, poll=args.poll)
    report["passed"] = report["post_check"]["passed"]
    if not probe or not report["passed"]:
        return report
    report["stage"] = "barrier_probe"
    barrier_id = str(uuid.uuid4())
    report["barrier_probe"] = barrier_probe(write_probe(probe_dsn),
                                            lambda: read_one(target_dsn, "barrier_rows", {"barrier_id": barrier_id}),
                                            barrier_id, timeout=args.probe_timeout, poll=args.probe_poll)
    # The probe row is the first new write since the checks passed, so what it caused has to be read again.
    report["post_check_after_probe"] = post_check(baseline, status, rows, timeout=args.probe_timeout, settle=0, poll=args.poll)
    report["passed"] = report["barrier_probe"]["passed"] and report["post_check_after_probe"]["passed"]
    return report


def main(argv, environ):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="enable the forward subscription if it is off, and send one barrier row through it")
    parser.add_argument("--source-system-id", help="required with --apply: the source identifier printed by a dry run")
    parser.add_argument("--target-system-id", help="required with --apply: the target identifier printed by a dry run")
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
    try:
        report = run(args, environ)
    except Exception as error:  # noqa: BLE001 - A traceback could print a driver message, so never let one out.
        report = {"passed": False, "stage": "unexpected", "error_category": describe(error)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
