#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.6"]
# ///
"""Reproduce the forward subscription turning itself off, and test the command that resumes it.

Disposable local containers only: nothing here can reach an actual database.

recover   Cut the link under writes, let rows pile up, resume with the real command, prove nothing was lost.
refusals  Every state the command must refuse, each undone again to show the refusal came from that state.
paths     The ways a resume goes wrong after ENABLE, and the ways it is run a second time.
timeouts  Which settings turn a lost link into a stopped subscription.
"""
import argparse
import ast
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
from lag_lab import (
    CONDITIONS,
    DB_LIMITS,
    DB_PARAMETERS,
    POSTGRES_IMAGE,
    Lab,
    build_shaper,
    docker,
    machine_load,
    remove_shaper,
)
from lag_metrics import condense, parse_log_events, summarize_timeout_trials
from replication_lag import (
    INSERT_MESSAGE,
    ROOT,
    ROWS,
    SUBSCRIPTION,
    connect,
    prepare,
    quiet_autovacuum,
    seed,
    subscribe,
    transaction_ids,
    wait_ready,
    write_load,
)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from replication import names
from replication import subscription as rendered_subscription

TOOL = HERE.parent / "resume_replication.py"
REVERSE = names("source")[2]
MARKER = "t8-lab-password-marker"
LOG_PARAMETERS = {"log_line_prefix": "%m %e "}
SMALL_ROWS = {"messages": 1_900, "attachments": 100}
# The incident: about 80 MiB between the source WAL and the slot's confirmed flush position.
BACKLOG_BYTES = 80 * 1024 * 1024
BACKLOG_BATCH_ROWS = 200
BODY_REPEATS = 56
TRIAL_OBSERVE_SECONDS = 45
TRIAL_HOLD_SECONDS = 3


def load_provision():
    path = ROOT / "services/cutover-executor/src/bravo_cutover/provision.py"
    spec = importlib.util.spec_from_file_location("bravo_provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def executor_schema():
    """The Executor's own DDL. Parsed rather than imported: importing it needs the service's dependencies."""
    tree = ast.parse((ROOT / "services/cutover-executor/src/bravo_cutover/store.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SCHEMA_SQL":
            return node.value.func.value.value.format(holding="'PENDING', 'RUNNING', 'SAFE_HOLD'")
    raise LookupError("SCHEMA_SQL not found")


def subscribe_reverse(lab, source):
    parameters = rendered_subscription("source")["spec"]["parameters"]
    options = ", ".join(f"{key} = '{value}'" for key, value in parameters.items())
    peer = lab.wan["target"]["ip"]
    source.execute(f"""CREATE SUBSCRIPTION {REVERSE}
        CONNECTION 'host={peer} port=5432 dbname=chat user=chat_replicator connect_timeout=10'
        PUBLICATION target_chat WITH ({options})""")


def build_pair(lab, source, target, rows):
    """The designed resting state: source LOGIN, target NOLOGIN, forward on, reverse prepared and off."""
    prepare(source, target)
    target.execute("ALTER ROLE chat_app NOLOGIN")
    seed(source, rows)
    subscribe(lab, target)
    wait_ready(target)
    subscribe_reverse(lab, source)
    provision = load_provision()
    for connection, subscription, publication in ((source, REVERSE, "source_chat"), (target, SUBSCRIPTION, "target_chat")):
        connection.execute("CREATE ROLE cutover_control LOGIN")
        connection.execute(provision.control_sql(subscription))
        connection.execute(f"ALTER PUBLICATION {publication} ADD TABLE cutover.barrier, cutover.probe")
    target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} REFRESH PUBLICATION WITH (copy_data = false)")
    source.execute("CREATE ROLE t8_probe LOGIN")
    source.execute("GRANT USAGE ON SCHEMA cutover TO t8_probe")
    source.execute("GRANT SELECT, INSERT, DELETE ON cutover.barrier TO t8_probe")
    source.execute("CREATE DATABASE executor_state")
    with psycopg.connect(lab.dsn("source", dbname="executor_state"), autocommit=True) as state:
        state.execute("CREATE SCHEMA executor")
        state.execute(executor_schema())


def run_tool(lab, *arguments, source=None, target=None, executor=None, probe=None, omit=()):
    """The real command as a separate process, the way an operator runs it. Each DSN carries a marker password."""
    def marked(dsn):
        return f"{dsn} password={MARKER}"

    settings = {"T8_RESUME_ALLOW_LOOPBACK_PLAINTEXT": "1",
                "T8_RESUME_SOURCE_DSN": marked(source or lab.dsn("source", "chat_replicator")),
                "T8_RESUME_TARGET_DSN": marked(target or lab.dsn("target")),
                "T8_RESUME_EXECUTOR_DSN": marked(executor or lab.dsn("source", dbname="executor_state")),
                "T8_RESUME_SOURCE_PROBE_DSN": marked(probe or lab.dsn("source", "t8_probe"))}
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("T8_RESUME_", "PG"))}
    environment.update({key: value for key, value in settings.items() if key not in omit})
    began = time.monotonic()
    done = subprocess.run([sys.executable, str(TOOL), *arguments], env=environment, capture_output=True, text=True, check=False)
    return {"exit": done.returncode, "seconds": round(time.monotonic() - began, 2),
            "credentials_in_output": MARKER in done.stdout + done.stderr,
            "report": json.loads(done.stdout) if done.stdout.strip() else {"unparsed_stderr_bytes": len(done.stderr)}}


def pins(report):
    preflight = report["preflight"]
    return ["--source-system-id", preflight["source"]["system_identifier"],
            "--target-system-id", preflight["target"]["system_identifier"]]


def subscription_row(target):
    return target.execute("""
        SELECT s.subenabled, w.pid, e.apply_error_count, e.sync_error_count
        FROM pg_subscription AS s
        LEFT JOIN pg_stat_subscription AS w ON w.subid = s.oid AND w.worker_type = 'apply'
        LEFT JOIN pg_stat_subscription_stats AS e ON e.subid = s.oid
        WHERE s.subname = %s""", (SUBSCRIPTION,)).fetchone()


def is_enabled(target):
    return subscription_row(target)[0]


def slot_row(source, slot=SUBSCRIPTION):
    return source.execute("""
        SELECT active, wal_status, invalidation_reason, (pg_current_wal_lsn() - confirmed_flush_lsn)::bigint
        FROM pg_replication_slots WHERE slot_name = %s""", (slot,)).fetchone()


def wait_until(condition, timeout, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def stop_cleanly(source, target):
    target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} DISABLE")
    if not wait_until(lambda: not slot_row(source)[0], 30):
        raise TimeoutError("slot was not released")


class Writer(threading.Thread):
    """50 writes per second until told to stop, the design's load. Keeps every acknowledged row id."""

    def __init__(self, lab):
        super().__init__(daemon=True)
        self.dsn = lab.dsn("source")
        self.halt = threading.Event()
        self.acks = []

    def run(self):
        with connect(self.dsn) as connection:
            self.acks, _ = write_load(connection, keep_going=lambda _: not self.halt.is_set())

    def stop(self):
        self.halt.set()
        self.join()
        return self.acks


def pile_up(source, wanted_bytes):
    """Many short transactions, as hours of ordinary writes would be, until the slot is that far behind."""
    while True:
        behind = slot_row(source)[3]
        if behind >= wanted_bytes:
            return behind
        source.execute("""
            INSERT INTO messages (idempotency_key, author, body)
            SELECT gen_random_uuid(), 'backlog', repeat(md5(g::text || clock_timestamp()::text), %s)
            FROM generate_series(1, %s) AS g""", (BODY_REPEATS, BACKLOG_BATCH_ROWS))


def table_digest(connection, table):
    return connection.execute(f"SELECT count(*), sum(hashtextextended(t::text, 0)::numeric) FROM {table} AS t").fetchone()


def verify_no_loss(source, target, acks, *, stopped_after_id, stopped_until_id):
    """With writes stopped: every acknowledged row, every row piled up, and every table identical."""
    newest = source.execute("SELECT max(id) FROM messages").fetchone()[0]
    arrived = wait_until(lambda: (target.execute("SELECT max(id) FROM messages").fetchone()[0] or 0) >= newest, 120)
    ids = [row_id for row_id, _ in acks]
    missing = target.execute("""SELECT count(*) FROM unnest(%s::bigint[]) AS a(id)
                                WHERE NOT EXISTS (SELECT FROM messages AS m WHERE m.id = a.id)""", (ids,)).fetchone()[0]

    def between(connection):
        return connection.execute("SELECT count(*) FROM messages WHERE id > %s AND id <= %s",
                                  (stopped_after_id, stopped_until_id)).fetchone()[0]

    piled_source, piled_target = between(source), between(target)
    return {"newest_row_arrived": arrived, "acknowledged_rows": len(ids), "acknowledged_rows_missing_on_target": missing,
            "rows_written_while_stopped": piled_source, "of_those_on_target": piled_target,
            "messages_identical": table_digest(source, "public.messages") == table_digest(target, "public.messages"),
            "attachments_identical": table_digest(source, "public.attachments") == table_digest(target, "public.attachments"),
            "barrier_rows_left": {"source": source.execute("SELECT count(*) FROM cutover.barrier").fetchone()[0],
                                  "target": target.execute("SELECT count(*) FROM cutover.barrier").fetchone()[0]}}


def resting_state(source, target):
    return {"target_app_login": target.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'").fetchone()[0],
            "source_app_login": source.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'").fetchone()[0],
            "reverse_enabled": source.execute("SELECT subenabled FROM pg_subscription WHERE subname = %s", (REVERSE,)).fetchone()[0]}


def recover_run(condition, repetition):
    with (Lab(CONDITIONS[condition], parameters=LOG_PARAMETERS) as lab,
          connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target):
        build_pair(lab, source, target, ROWS)
        writer = Writer(lab)
        writer.start()
        time.sleep(5)
        machine_before = machine_load()
        lines_before = len(lab.log_lines("target"))
        cut = time.monotonic()
        lab.link_down()
        turned_off = wait_until(lambda: not is_enabled(target), 30)
        disabled_after = round(time.monotonic() - cut, 2)
        if not turned_off:
            raise RuntimeError("subscription did not turn itself off")
        stopped_after_id = source.execute("SELECT max(id) FROM messages").fetchone()[0]
        time.sleep(3)  # The incident lost the link for good; this one keeps it down a little past the failure.
        lab.link_up()
        target_events = parse_log_events(lab.log_lines("target")[lines_before:])
        too_early = run_tool(lab, "--no-barrier-probe")
        backlog = pile_up(source, BACKLOG_BYTES)
        wait_until(lambda: not slot_row(source)[0], 30)
        enabled_flag, _, apply_errors, sync_errors = subscription_row(target)
        active, wal_status, invalidation, behind = slot_row(source)
        signature = {"subscription_enabled": enabled_flag, "apply_error_count": apply_errors, "sync_error_count": sync_errors,
                     "slot_active": active, "slot_wal_status": wal_status, "slot_invalidation_reason": invalidation,
                     "behind_bytes": behind, **resting_state(source, target)}
        dry = run_tool(lab)
        still_off_after_dry_run = not is_enabled(target)
        stopped_until_id = source.execute("SELECT max(id) FROM messages").fetchone()[0]
        applied = run_tool(lab, "--apply", *pins(dry["report"]))
        verify_again = run_tool(lab, "--settle", "2")
        acks = writer.stop()
        loss = verify_no_loss(source, target, acks, stopped_after_id=stopped_after_id, stopped_until_id=stopped_until_id)
        checks = {
            "disabled_like_the_incident": signature["apply_error_count"] == 1 and signature["sync_error_count"] == 0
            and not signature["subscription_enabled"] and signature["slot_wal_status"] in ("reserved", "extended")
            and signature["slot_invalidation_reason"] is None and bool(target_events)
            and target_events[0]["sqlstate"] == "08006",
            "dry_run_changed_nothing": dry["exit"] == 0 and still_off_after_dry_run,
            "apply_passed": applied["exit"] == 0,
            "enabled_by_this_run": applied["report"].get("enabled_by") in ("control_function", "alter_subscription"),
            "barrier_went_both_ways": bool(applied["report"].get("barrier_probe", {}).get("passed")),
            "second_run_only_verified": verify_again["exit"] == 0 and verify_again["report"].get("would_run") is None,
            "no_acknowledged_row_lost": loss["acknowledged_rows_missing_on_target"] == 0 and loss["newest_row_arrived"],
            "rows_written_while_stopped_arrived": 0 < loss["rows_written_while_stopped"] == loss["of_those_on_target"],
            "tables_identical": loss["messages_identical"] and loss["attachments_identical"],
            "no_probe_row_left": loss["barrier_rows_left"] == {"source": 0, "target": 0},
            "single_writer_kept": resting_state(source, target) == {
                "target_app_login": False, "source_app_login": True, "reverse_enabled": False},
            "no_credentials_in_output": not any(run["credentials_in_output"] for run in (too_early, dry, applied, verify_again)),
        }
        return {"condition": condition, "repetition": repetition, "disabled_after_link_cut_s": disabled_after,
                "target_log_events": target_events, "signature_before_resume": signature, "backlog_bytes": backlog,
                "dry_run_right_after_link_up": too_early, "dry_run": dry, "apply": applied, "second_run_default": verify_again,
                "no_loss": loss, "machine": {"before": machine_before, "after": machine_load()},
                "checks": checks, "passed": all(checks.values())}


class Case:
    """A state to refuse: arrange it, confirm the refusal, undo it, confirm the same command passes again."""

    def __init__(self, name, reason, arrange=None, undo=None, **tool):
        self.name, self.reason, self.arrange, self.undo, self.tool = name, reason, arrange, undo, tool


def open_session(lab, side, user):
    return psycopg.connect(lab.dsn(side, user), autocommit=True)


def refusal_cases(lab, source, target, state_dsn):
    held = {}

    def execution(status):
        def arrange():
            with psycopg.connect(state_dsn, autocommit=True) as state:
                state.execute("""INSERT INTO executor.executions
                    (execution_id, idempotency_key, request_digest, scope, plan_id, epoch, request, status)
                    VALUES (gen_random_uuid(), %s, 'lab', 'lab', 'lab', 1, '{}', %s)""", (str(uuid.uuid4()), status))

        return arrange

    def clear_executions():
        with psycopg.connect(state_dsn, autocommit=True) as state:
            state.execute("DELETE FROM executor.executions")

    def app_session_arrange():
        target.execute("ALTER ROLE chat_app LOGIN")
        held["session"] = open_session(lab, "target", "chat_app")
        target.execute("ALTER ROLE chat_app NOLOGIN")

    def session_close():
        held.pop("session").close()

    def control_session():
        held["session"] = open_session(lab, "source", "cutover_control")

    def reverse_on():
        source.execute(f"ALTER SUBSCRIPTION {REVERSE} ENABLE")
        wait_until(lambda: source.execute("SELECT count(pid) FROM pg_stat_subscription WHERE subname = %s", (REVERSE,)).fetchone()[0], 15)

    def reverse_off():
        source.execute(f"ALTER SUBSCRIPTION {REVERSE} DISABLE")
        wait_until(lambda: not slot_row(target, REVERSE)[0], 15)

    def ahead_arrange():
        target.execute("INSERT INTO messages (id, idempotency_key, author, body) VALUES (2000000, %s, 'ahead', 'x')", (uuid.uuid4(),))

    def ahead_undo():
        target.execute("DELETE FROM messages WHERE id = 2000000")

    def slot_reader_on():
        docker("exec", "-d", lab.names["source"], "pg_recvlogical", "-U", "postgres", "-d", "chat", "-S", SUBSCRIPTION, "--start",
               "-n", "-f", "/dev/null", "-o", "proto_version=4", "-o", "publication_names=source_chat")
        if not wait_until(lambda: slot_row(source)[0], 15):
            raise TimeoutError("test reader did not take the slot")

    def slot_reader_off():
        source.execute("SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots WHERE slot_name = %s", (SUBSCRIPTION,))
        wait_until(lambda: not slot_row(source)[0], 15)

    wrong_port = "host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1"
    chat_replicator_target = lab.dsn("target", "chat_replicator")
    return [
        Case("reverse subscription running", "reverse_active", reverse_on, reverse_off),
        Case("target application can log in", "target_app_can_login", lambda: target.execute("ALTER ROLE chat_app LOGIN"),
             lambda: target.execute("ALTER ROLE chat_app NOLOGIN")),
        Case("target application session left open", "target_app_sessions", app_session_arrange, session_close),
        Case("source application fenced", "source_app_cannot_login", lambda: source.execute("ALTER ROLE chat_app NOLOGIN"),
             lambda: source.execute("ALTER ROLE chat_app LOGIN")),
        Case("execution PENDING", "executor_running", execution("PENDING"), clear_executions),
        Case("execution RUNNING", "executor_running", execution("RUNNING"), clear_executions),
        Case("execution SAFE_HOLD", "executor_running", execution("SAFE_HOLD"), clear_executions),
        Case("no access to the execution record", "executor_state_unconfirmed", omit=("T8_RESUME_EXECUTOR_DSN",)),
        Case("execution record unreachable", "executor_state_unconfirmed", executor=wrong_port),
        Case("execution record has no such table", "executor_state_unconfirmed", executor=lab.dsn("target")),
        Case("control session on a database", "control_session_present", control_session, session_close),
        Case("no privilege to enable", "no_enable_privilege", target=chat_replicator_target),
        Case("another reader holds the slot", "slot_in_use", slot_reader_on, slot_reader_off),
        Case("rows on the target the source lacks", "target_ahead_of_source", ahead_arrange, ahead_undo),
        Case("source and target swapped", "slot_missing", source=lab.dsn("target"), target=lab.dsn("source", "chat_replicator")),
        Case("both DSNs on one cluster", "source_and_target_are_one_cluster", source=lab.dsn("target")),
        Case("source unreachable", "source_unconfirmed", source=wrong_port),
        Case("target unreachable", "target_unconfirmed", target=wrong_port),
        Case("remote host without verified TLS", "source_unconfirmed", source="host=db.example.invalid dbname=chat user=x"),
        Case("probe role without DELETE", "barrier_probe_not_permitted", probe=lab.dsn("source", "cutover_control")),
        Case("no barrier in the publication", "barrier_not_published",
             lambda: source.execute("ALTER PUBLICATION source_chat DROP TABLE cutover.barrier"),
             lambda: source.execute("ALTER PUBLICATION source_chat ADD TABLE cutover.barrier")),
    ]


def run_refusals():
    with (Lab(CONDITIONS["unlimited"], parameters=LOG_PARAMETERS) as lab,
          connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target):
        build_pair(lab, source, target, SMALL_ROWS)
        stop_cleanly(source, target)
        control = run_tool(lab)
        identity = pins(control["report"])
        passes_as_designed = control["exit"] == 0 and control["report"]["refusals"] == []
        quiet_autovacuum(source)
        quiet_autovacuum(target)
        before = (transaction_ids(source), transaction_ids(target), is_enabled(target))
        repeated = [run_tool(lab)["exit"] for _ in range(3)]
        after = (transaction_ids(source), transaction_ids(target), is_enabled(target))
        source.execute(INSERT_MESSAGE, (uuid.uuid4(), "control"))
        counter_moves = transaction_ids(source) != after[0]
        results = []
        for case in refusal_cases(lab, source, target, lab.dsn("source", dbname="executor_state")):
            if case.arrange:
                case.arrange()
            refused = run_tool(lab, "--apply", *identity, **case.tool)
            stayed_off = not is_enabled(target)
            if case.undo:
                case.undo()
            passed_again = run_tool(lab)
            results.append({"case": case.name, "reason": case.reason, "exit": refused["exit"],
                            "refusals": refused["report"].get("refusals"), "subscription_stayed_off": stayed_off,
                            "passes_once_undone": passed_again["exit"] == 0, "credentials_in_output": refused["credentials_in_output"],
                            "passed": (refused["exit"] == 1 and case.reason in (refused["report"].get("refusals") or [])
                                       and stayed_off and passed_again["exit"] == 0 and not refused["credentials_in_output"])})
            print(json.dumps({"case": case.name, "passed": results[-1]["passed"]}, ensure_ascii=False), flush=True)
        controls = {"no_execution_record_but_declared": run_tool(lab, "--executor-not-deployed", omit=("T8_RESUME_EXECUTOR_DSN",))["exit"] == 0}
        with psycopg.connect(lab.dsn("source", dbname="executor_state"), autocommit=True) as state:
            state.execute("""INSERT INTO executor.executions
                (execution_id, idempotency_key, request_digest, scope, plan_id, epoch, request, status)
                VALUES (gen_random_uuid(), 'done', 'lab', 'lab', 'lab', 1, '{}', 'DONE')""")
        controls["finished_execution_does_not_block"] = run_tool(lab)["exit"] == 0
        wrong_identity = run_tool(lab, "--source-system-id", identity[1], "--target-system-id", "1")
        unpinned = run_tool(lab, "--apply")
        mismatch = {"wrong_identifier": "target_identity_mismatch" in wrong_identity["report"]["refusals"],
                    "apply_without_identifiers": "source_identity_not_pinned" in unpinned["report"]["refusals"],
                    "subscription_stayed_off": not is_enabled(target)}
        # Last, because it cannot be undone: the slot is gone for good.
        source.execute("ALTER SYSTEM SET max_slot_wal_keep_size = '1MB'")
        source.execute("SELECT pg_reload_conf()")

        def invalidated():
            source.execute("INSERT INTO messages (idempotency_key, author, body) SELECT gen_random_uuid(), 'wal', repeat('x', 1800) FROM generate_series(1, 1000)")
            source.execute("SELECT pg_switch_wal()")
            source.execute("CHECKPOINT")
            return slot_row(source)[2] is not None

        reached = wait_until(invalidated, 120, interval=0.1)
        lost = run_tool(lab, "--apply", *identity)
        slot_invalidated = {"invalidation_reached": reached, "reason_seen": slot_row(source)[2], "exit": lost["exit"],
                            "refusals": lost["report"].get("refusals"), "subscription_stayed_off": not is_enabled(target),
                            "passed": lost["exit"] == 1 and "slot_invalidated" in (lost["report"].get("refusals") or []) and not is_enabled(target)}
        read_only = {"repeated_exits": repeated, "transaction_ids_unchanged": before == after, "counter_moves_on_a_real_write": counter_moves}
        passed = (passes_as_designed and all(case["passed"] for case in results) and all(controls.values()) and all(mismatch.values())
                  and slot_invalidated["passed"] and before == after and counter_moves and repeated == [0, 0, 0])
        return {"control_dry_run_passed": passes_as_designed, "dry_run_read_only": read_only, "cases": results, "controls": controls,
                "identity": mismatch, "slot_invalidated": slot_invalidated, "machine": machine_load(), "passed": passed}


def run_paths():
    outcome = {}
    with (Lab(CONDITIONS["20mbit-rtt30"], parameters=LOG_PARAMETERS) as lab,
          connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target):
        build_pair(lab, source, target, SMALL_ROWS)
        identity = pins(run_tool(lab, "--settle", "1")["report"])
        apply_fast = ["--apply", *identity, "--settle", "3"]

        # Someone else already resumed it: never enabled again, and the default writes nothing.
        stop_cleanly(source, target)
        target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} ENABLE")
        default_run = run_tool(lab, "--settle", "3")
        applied_run = run_tool(lab, *apply_fast)
        outcome["resumed_by_hand_first"] = {
            "default": default_run, "apply": applied_run,
            "passed": default_run["exit"] == 0 and default_run["report"]["would_run"] is None and "barrier_probe" not in default_run["report"]
            and applied_run["exit"] == 0 and applied_run["report"]["enabled_by"] is None and applied_run["report"]["barrier_probe"]["passed"]}

        # The link is still down when the command runs: nothing to confirm, so it fails at the deadline.
        stop_cleanly(source, target)
        lab.link_down()
        no_link = run_tool(lab, "--apply", *identity, "--no-barrier-probe", "--timeout", "25", "--settle", "2")
        stayed_enabled = is_enabled(target)
        lab.link_up()
        wait_until(lambda: slot_row(source)[0], 40)
        rerun = run_tool(lab, *apply_fast)
        outcome["link_still_down"] = {
            "first": no_link, "stayed_enabled": stayed_enabled, "rerun": rerun,
            "passed": no_link["exit"] == 1 and no_link["report"]["post_check"]["reason"] == "deadline"
            and not no_link["report"]["post_check"]["checks"]["publisher_connected"]
            and rerun["exit"] == 0 and rerun["report"]["enabled_by"] is None}

        # The link is lost again during catch-up: it stops at once, then resumes once more without loss.
        stop_cleanly(source, target)
        behind = pile_up(source, BACKLOG_BYTES)
        newest = source.execute("SELECT max(id) FROM messages").fetchone()[0]
        found = {}

        def run_apply():
            found["first"] = run_tool(lab, *apply_fast)

        worker = threading.Thread(target=run_apply)
        worker.start()
        started = wait_until(lambda: slot_row(source)[0], 30)
        time.sleep(3)
        lab.link_down()
        worker.join()
        lab.link_up()
        turned_off = not is_enabled(target)
        wait_until(lambda: not slot_row(source)[0], 30)
        again = run_tool(lab, *apply_fast)
        arrived = (target.execute("SELECT max(id) FROM messages").fetchone()[0] or 0) >= newest
        first = found["first"]
        outcome["link_lost_during_catch_up"] = {
            "backlog_bytes": behind, "first": first, "turned_itself_off": turned_off, "again": again, "rows_arrived": arrived,
            "passed": started and first["exit"] == 1 and first["report"]["post_check"]["reason"] == "failed_check"
            and turned_off and again["exit"] == 0 and arrived}

        # An apply error rather than a lost link: same flag, different cause, and retrying alone cannot fix it.
        stop_cleanly(source, target)
        row_id = source.execute(INSERT_MESSAGE, (uuid.uuid4(), "conflict")).fetchone()[0]
        target.execute("INSERT INTO messages (id, idempotency_key, author, body) VALUES (%s, %s, 'conflict', 'x')", (row_id, uuid.uuid4()))
        conflict = run_tool(lab, *apply_fast)
        target.execute("DELETE FROM messages WHERE id = %s AND author = 'conflict' AND body = 'x'", (row_id,))
        wait_until(lambda: not slot_row(source)[0], 30)
        healed = run_tool(lab, *apply_fast)
        outcome["apply_conflict"] = {
            "first": conflict, "after_removing_the_row": healed,
            "passed": conflict["exit"] == 1 and conflict["report"]["post_check"]["reason"] == "failed_check"
            and healed["exit"] == 0}

        # A publication that drops deletes: the row arrives, the delete does not, and only the probe can tell.
        source.execute("ALTER PUBLICATION source_chat SET (publish = 'insert, update, truncate')")
        no_delete = run_tool(lab, *apply_fast, "--probe-timeout", "5")
        source.execute("ALTER PUBLICATION source_chat SET (publish = 'insert, update, delete, truncate')")
        target.execute("DELETE FROM cutover.barrier")
        probe = no_delete["report"].get("barrier_probe", {})
        outcome["delete_not_replicated"] = {
            "run": no_delete, "passed": no_delete["exit"] == 1 and probe.get("arrived_ms") is not None and probe.get("delete_arrived_ms") is None}

        # The Executor's narrow role instead of the subscription owner.
        stop_cleanly(source, target)
        narrow = run_tool(lab, *apply_fast, target=lab.dsn("target", "cutover_control"))
        outcome["control_function_role"] = {
            "run": narrow, "passed": narrow["exit"] == 0 and narrow["report"]["enabled_by"] == "control_function"}

        # An environment without the control function: the subscription owner's ALTER is the fallback.
        stop_cleanly(source, target)
        target.execute("DROP FUNCTION cutover.set_subscription(text, boolean)")
        owner = run_tool(lab, *apply_fast)
        outcome["owner_without_control_function"] = {
            "run": owner, "passed": owner["exit"] == 0 and owner["report"]["enabled_by"] == "alter_subscription"}

        # Reading is enough for the read-only roles the tailnet actually lets through.
        replicator = run_tool(lab, "--settle", "2", target=lab.dsn("target", "chat_replicator"))
        outcome["verify_with_replication_role"] = {"run": replicator, "passed": replicator["exit"] == 0}

        outcome["no_credentials_in_output"] = MARKER not in json.dumps(outcome, ensure_ascii=False)
        outcome["machine"] = machine_load()
    return outcome


# Each setting is run against the same link failures, three times by default.
SWEEP = [(5, 5, True, seconds, 100) for seconds in (2, 3, 4, 5, 6, 8)]
SETTINGS = [(5, 5, True), (30, 5, True), (60, 5, True), (5, 30, True), (30, 30, True), (60, 60, True), (5, 5, False), (60, 60, False)]
KNOBS = [(*setting, seconds, 100) for setting in SETTINGS for seconds in (8, 20) if (setting, seconds) != ((5, 5, True), 8)]
LONG = [(30, 30, True, 40, 100), (60, 60, True, 40, 100), (5, 5, False, 40, 100)]
LOSSY = [(5, 5, True, 30, 30), (60, 60, True, 30, 30)]
TRIALS = SWEEP + KNOBS + LONG + LOSSY


def apply_settings(source, target, receiver, sender, disable_on_error):
    stop_cleanly(source, target) if is_enabled(target) else wait_until(lambda: not slot_row(source)[0], 90)
    for connection in (source, target):
        connection.execute(f"ALTER SYSTEM SET wal_receiver_timeout = '{receiver}s'")
        connection.execute(f"ALTER SYSTEM SET wal_sender_timeout = '{sender}s'")
        connection.execute("SELECT pg_reload_conf()")
    target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} SET (disable_on_error = {'true' if disable_on_error else 'false'})")
    target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} ENABLE")
    if not wait_until(lambda: slot_row(source)[0] and subscription_row(target)[1], 30):
        raise TimeoutError("worker did not start")


def run_trial(lab, source, target, receiver, sender, disable_on_error, seconds, loss, repetition):
    apply_settings(source, target, receiver, sender, disable_on_error)
    writer = Writer(lab)
    writer.start()
    time.sleep(3)
    _, worker_before, errors_before, _ = subscription_row(target)
    lines_before = {side: len(lab.log_lines(side)) for side in ("source", "target")}
    cut = time.monotonic()
    lab.link_down(loss)
    disabled_after, restored = None, None

    def poll_until(end):
        nonlocal disabled_after
        while time.monotonic() < end and disabled_after is None:
            if not is_enabled(target):
                disabled_after = round(time.monotonic() - cut, 2)
            time.sleep(0.05)

    poll_until(cut + seconds)
    lab.link_up()
    restored = time.monotonic()
    newest = source.execute("SELECT max(id) FROM messages").fetchone()[0]
    flowing_after, outcome = None, "no_recovery_seen"
    deadline = restored + TRIAL_OBSERVE_SECONDS
    while time.monotonic() < deadline and disabled_after is None:
        enabled, worker, _, _ = subscription_row(target)
        if not enabled:
            disabled_after = round(time.monotonic() - cut, 2)
            break
        if worker and (target.execute("SELECT max(id) FROM messages").fetchone()[0] or 0) >= newest:
            flowing_after = round(time.monotonic() - restored, 2)
            poll_until(time.monotonic() + TRIAL_HOLD_SECONDS)
            break
        time.sleep(0.05)
    if disabled_after is not None:
        outcome, flowing_after = "disabled", None
    elif flowing_after is not None:
        outcome = "recovered"
    writer.stop()
    enabled, worker_after, errors_after, _ = subscription_row(target)
    return {"receiver_s": receiver, "sender_s": sender, "disable_on_error": disable_on_error, "blip_s": seconds, "loss_pct": loss,
            "repetition": repetition, "outcome": outcome, "disabled_after_s": disabled_after,
            "rows_flowing_after_link_up_s": flowing_after, "worker_replaced": worker_after != worker_before,
            "apply_errors_added": errors_after - errors_before,
            "target_log_events": parse_log_events(lab.log_lines("target")[lines_before["target"]:]),
            "source_log_events": parse_log_events(lab.log_lines("source")[lines_before["source"]:])}


def save(path, results):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    temporary.replace(path)


def load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def command_recover(args):
    results = load(args.out)
    for condition in args.conditions:
        for repetition in range(1, args.reps + 1):
            runs = results.setdefault("recover", {}).setdefault(condition, [])
            if len(runs) >= repetition:
                continue
            run = recover_run(condition, repetition)
            runs.append(run)
            save(args.out, results)
            print(json.dumps({"condition": condition, "repetition": repetition, "passed": run["passed"], "checks": run["checks"]}), flush=True)


def command_refusals(args):
    results = load(args.out)
    results["refusals"] = run_refusals()
    save(args.out, results)
    print(json.dumps({"passed": results["refusals"]["passed"]}), flush=True)


def command_paths(args):
    results = load(args.out)
    results["paths"] = run_paths()
    save(args.out, results)
    print(json.dumps({key: value["passed"] for key, value in results["paths"].items() if isinstance(value, dict) and "passed" in value}), flush=True)


def command_timeouts(args):
    results = load(args.out)
    entry = results.setdefault("timeouts", {"condition": args.condition, "trials": [], "load_samples": []})
    with (Lab(CONDITIONS[args.condition], parameters=LOG_PARAMETERS) as lab,
          connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target):
        build_pair(lab, source, target, SMALL_ROWS)
        for setting in TRIALS[args.shard::args.shards]:
            for repetition in range(1, args.reps + 1):
                done = [t for t in entry["trials"] if (t["receiver_s"], t["sender_s"], t["disable_on_error"], t["blip_s"], t["loss_pct"], t["repetition"]) == (*setting, repetition)]
                if done:
                    continue
                trial = run_trial(lab, source, target, *setting, repetition)
                entry["trials"].append(trial)
                entry["load_samples"].append(machine_load())
                entry["summary"] = summarize_timeout_trials(entry["trials"])
                save(args.out, results)
                print(json.dumps({key: trial[key] for key in ("receiver_s", "sender_s", "disable_on_error", "blip_s", "loss_pct", "repetition", "outcome", "disabled_after_s", "rows_flowing_after_link_up_s")}), flush=True)


def summarize_recover(runs):
    def column(read):
        return condense([read(run) for run in runs])

    return {"runs": len(runs), "passed": sum(run["passed"] for run in runs),
            "backlog_bytes": column(lambda run: run["backlog_bytes"]),
            "disabled_after_link_cut_s": column(lambda run: run["disabled_after_link_cut_s"]),
            "backlog_cleared_s": column(lambda run: run["apply"]["report"]["post_check"]["backlog_cleared_seconds"]),
            "barrier_arrived_ms": column(lambda run: run["apply"]["report"]["barrier_probe"]["arrived_ms"]),
            "barrier_delete_arrived_ms": column(lambda run: run["apply"]["report"]["barrier_probe"]["delete_arrived_ms"]),
            "rows_written_while_stopped": column(lambda run: run["no_loss"]["rows_written_while_stopped"]),
            "acknowledged_rows_missing": column(lambda run: run["no_loss"]["acknowledged_rows_missing_on_target"])}


def command_assemble(args):
    """Parts ran side by side, so the final file is put together afterwards, in the order of the trial list."""
    merged = {"recover": {}, "timeouts": {"trials": [], "load_samples": []}}
    for part in map(load, args.parts):
        merged["recover"].update(part.pop("recover", {}))
        timeouts = part.pop("timeouts", None)
        if timeouts:
            merged["timeouts"]["condition"] = timeouts["condition"]
            merged["timeouts"]["trials"] += timeouts["trials"]
            merged["timeouts"]["load_samples"] += timeouts["load_samples"]
        merged.update(part)
    merged["timeouts"]["trials"].sort(key=lambda t: (TRIALS.index((t["receiver_s"], t["sender_s"], t["disable_on_error"], t["blip_s"], t["loss_pct"])), t["repetition"]))
    merged["timeouts"]["summary"] = summarize_timeout_trials(merged["timeouts"]["trials"])
    merged["recover_summary"] = {condition: summarize_recover(runs) for condition, runs in merged["recover"].items()}
    files = [str(path.relative_to(ROOT)) for path in (TOOL, Path(__file__).resolve(), HERE / "lag_lab.py", HERE / "lag_metrics.py", HERE / "lag_readonly.py")]
    merged["measured_code"] = json.loads(subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/evidence_sources.py"), "--repo", str(ROOT), "create", "--revision", args.revision, *files]))
    merged["environment"] = {"postgres_image": POSTGRES_IMAGE, "db_limits": list(DB_LIMITS), "log_parameters": LOG_PARAMETERS,
                             "resting_timeouts": {"wal_receiver_timeout": DB_PARAMETERS["wal_receiver_timeout"],
                                                  "wal_sender_timeout": DB_PARAMETERS["wal_sender_timeout"]},
                             "conditions": CONDITIONS, "all_links_assumed": True}
    save(args.out, merged)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--out", type=Path, required=True)
    recover.add_argument("--conditions", nargs="+", choices=list(CONDITIONS), default=list(CONDITIONS))
    recover.add_argument("--reps", type=int, default=3)
    recover.set_defaults(run=command_recover)
    refusals = commands.add_parser("refusals")
    refusals.add_argument("--out", type=Path, required=True)
    refusals.set_defaults(run=command_refusals)
    paths = commands.add_parser("paths")
    paths.add_argument("--out", type=Path, required=True)
    paths.set_defaults(run=command_paths)
    timeouts = commands.add_parser("timeouts")
    timeouts.add_argument("--out", type=Path, required=True)
    timeouts.add_argument("--condition", choices=list(CONDITIONS), default="100mbit-rtt30")
    timeouts.add_argument("--reps", type=int, default=3)
    timeouts.add_argument("--shard", type=int, default=0, help="run every --shards-th setting from this one")
    timeouts.add_argument("--shards", type=int, default=1)
    timeouts.set_defaults(run=command_timeouts)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--out", type=Path, required=True)
    assemble.add_argument("--revision", required=True, help="the full commit the parts were measured at")
    assemble.add_argument("parts", nargs="+", type=Path)
    assemble.set_defaults(run=command_assemble)
    args = parser.parse_args()
    if args.command == "assemble":
        args.run(args)
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # Cleanup runs in finally blocks.
    build_shaper()
    try:
        args.run(args)
    except psycopg.Error as error:
        raise SystemExit(f"database error: {type(error).__name__}") from None
    finally:
        remove_shaper()


if __name__ == "__main__":
    main()
