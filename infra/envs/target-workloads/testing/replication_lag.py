#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""Measure native logical replication lag against the T8 write-pause budget.

lab       Disposable local containers only: initial copy, lag under writes, catch-up after writes stop.
validate  Prove the measurement responds: pausing the subscription must raise lag and catch-up,
          and observe must stay read-only.
observe   Read-only. Samples two existing databases whose DSNs come from the environment.
"""
import argparse
import hashlib
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
    minimum_transfer_ms,
    remove_shaper,
)
from lag_metrics import (
    budget_scenarios,
    condense,
    first_reach,
    gate_lag_bytes,
    intervals_ms,
    parse_lsn,
    row_lags,
    summarize,
)
from lag_readonly import (
    STATEMENTS,
    connect_read_only,
    dsns_from_environment,
    observe,
    read_source,
    read_target,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE_SCHEMA = "infra/envs/source-workloads/schema.sql"
TARGET_SCHEMA = "infra/envs/target-workloads/schema.sql"
TARGET_SCHEMA_REF = "origin/feature/T8-replication-runtime"
SUBSCRIPTION = "t8_source_to_target"
# Same parameters as the #131 forward subscription (replication.py), except TLS and password,
# which this disposable pair does not have.
SUBSCRIPTION_OPTIONS = ("enabled=true, copy_data=true, create_slot=true, "
                        f"slot_name='{SUBSCRIPTION}', origin=none, binary=false, streaming=on, "
                        "disable_on_error=true, run_as_owner=false")
INSERT_MESSAGE = "INSERT INTO messages (idempotency_key, author, body) VALUES (%s, 'lag-load', %s) RETURNING id"
WRITE_RATE = 50  # Design 5.3.
# 98,000 + 2,000 rows at 1,800 characters: about 200 MB, with four rows per page and no TOAST.
ROWS = {"messages": 98_000, "attachments": 2_000}
BODY_CHARS = 1_800
BUDGET_MS = 5_000
# T13 DB-side comparison, source plus target: unlimited and 100/20 Mbps RTT 30 ms, proxy-limited
# assumed links, before collation checks (Notion T13 page, 11/3 comparison). Not measured here.
COMPARE_MS = (600, 1_300)
GATE_BYTES = 1024 * 1024  # bravo_cutover Settings.max_lag_bytes.
EXECUTOR_POLL_MS = 50  # bravo_cutover runner: time.sleep(0.05) in the barrier wait.
STATS_FLUSH_SECONDS = 2  # Cumulative statistics reach shared memory about once a second.


def schema_text(path, ref=None):
    file = ROOT / path
    if file.exists():
        return file.read_text()
    return subprocess.check_output(["git", "-C", str(ROOT), "show", f"{ref}:{path}"], text=True)


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True)


def prepare(source, target):
    for connection, schema in ((source, schema_text(SOURCE_SCHEMA)), (target, schema_text(TARGET_SCHEMA, TARGET_SCHEMA_REF))):
        connection.execute("CREATE ROLE chat_app LOGIN")
        connection.execute("CREATE ROLE chat_replicator LOGIN REPLICATION")
        connection.execute(schema)


def seed(source, rows):
    """Bulk rows of the shape the app writes. Hex content so wire size matches stored size."""
    source.execute("""
        INSERT INTO attachments (id, object_key, filename, content_type, size)
        SELECT gen_random_uuid(), 'seed/' || g, 'file-' || g || '.bin', 'application/octet-stream', 1024
        FROM generate_series(1, %s) AS g""", (rows["attachments"],))
    source.execute("""
        WITH numbered AS (SELECT id, row_number() OVER () AS n FROM attachments)
        INSERT INTO messages (idempotency_key, author, body, attachment_id)
        SELECT gen_random_uuid(), 'seed-' || g %% 50, repeat(md5(g::text), %(repeats)s), numbered.id
        FROM generate_series(1, %(messages)s) AS g
        LEFT JOIN numbered ON g %% %(every)s = 0 AND numbered.n = g / %(every)s""",
                   {"repeats": BODY_CHARS // 32, "messages": rows["messages"], "every": rows["messages"] // rows["attachments"]})
    source.execute("ANALYZE")
    source.execute("CHECKPOINT")  # Keep the seed's checkpoint out of the windows being measured.
    return int(source.execute("""
        SELECT sum(pg_total_relation_size(c)) FROM unnest(ARRAY['attachments'::regclass, 'messages'::regclass]) AS c""").fetchone()[0])


def subscribe(lab, target):
    peer = lab.wan["source"]["ip"]
    target.execute(f"""CREATE SUBSCRIPTION {SUBSCRIPTION}
        CONNECTION 'host={peer} port=5432 dbname=chat user=chat_replicator connect_timeout=10 application_name=bravo_t8_target_subscriber'
        PUBLICATION source_chat WITH ({SUBSCRIPTION_OPTIONS})""")


def wait_for_row(target, row_id, timeout=10):
    deadline = time.monotonic() + timeout
    while (target.execute("SELECT max(id) FROM messages").fetchone()[0] or 0) < row_id:
        if time.monotonic() > deadline:
            raise TimeoutError("row did not reach the target")
        time.sleep(0.01)


def wait_ready(target, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tables, ready, enabled = target.execute("""
            SELECT count(*), count(*) FILTER (WHERE srsubstate = 'r'),
                   (SELECT subenabled FROM pg_subscription WHERE subname = %s)
            FROM pg_subscription_rel""", (SUBSCRIPTION,)).fetchone()
        if not enabled:
            raise RuntimeError("subscription disabled itself on error")
        if tables and tables == ready:
            return time.monotonic()
        time.sleep(0.02)
    raise TimeoutError("initial copy did not finish")


def copy_initial_data(lab, source, target):
    sent_before = lab.tx_bytes("source")
    began = time.monotonic()
    subscribe(lab, target)
    finished = wait_ready(target)
    wire_bytes = lab.tx_bytes("source") - sent_before
    mbit = lab.shaping["mbit"] if lab.shaping else None
    return {"ms": round((finished - began) * 1000, 1), "wire_bytes": wire_bytes,
            "minimum_possible_ms": minimum_transfer_ms(wire_bytes, mbit)}


class Poller(threading.Thread):
    """One loop reads the target, then the source: the Executor's gate reads them in this order."""

    def __init__(self, lab, interval=0.002):
        super().__init__(daemon=True)
        self.dsns = (lab.dsn("source"), lab.dsn("target"))
        self.interval = interval
        self.source, self.target = [], []
        self.finished = threading.Event()
        self.until = None
        self.error = None

    def run(self):
        def querier(connection):
            return lambda name, params: connection.execute(STATEMENTS[name], params).fetchone()

        try:
            with connect(self.dsns[0]) as source, connect(self.dsns[1]) as target:
                while not self.finished.is_set():
                    target_sample = read_target(querier(target), SUBSCRIPTION)
                    target_sample["t"] = time.monotonic()
                    source_sample = read_source(querier(source), SUBSCRIPTION)
                    source_sample["t"] = time.monotonic()
                    self.target.append(target_sample)
                    self.source.append(source_sample)
                    if self.until and self.until(target_sample, source_sample):
                        return
                    time.sleep(self.interval)
        except (psycopg.Error, LookupError) as error:
            self.error = error

    def wait_until(self, condition, timeout):
        """Let sampling end itself once condition holds, or stop it at the timeout."""
        self.until = condition
        self.join(timeout)
        self.finished.set()
        self.join()
        if self.error:
            raise self.error

    def seen(self):
        return [(sample["t"], sample["max_id"]) for sample in self.target]

    def positions(self, side, field):
        return [(sample["t"], sample[field]) for sample in (self.source if side == "source" else self.target)]


def write_load(connection, *, seconds=None, rate=WRITE_RATE, keep_going=None):
    """Open-loop writer: the schedule does not slow down when a commit does."""
    acks, latencies = [], []
    period, started = 1 / rate, time.monotonic()
    while True:
        index = len(acks)
        elapsed = time.monotonic() - started
        if (seconds is not None and index >= seconds * rate) or (keep_going and not keep_going(elapsed)):
            break
        delay = started + index * period - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        began = time.monotonic()
        row = connection.execute(INSERT_MESSAGE, (uuid.uuid4(), os.urandom(BODY_CHARS // 2).hex())).fetchone()
        acked = time.monotonic()
        acks.append((row[0], acked))
        latencies.append((acked - began) * 1000)
    return acks, latencies


def achieved_rate(acks):
    return round((len(acks) - 1) / (acks[-1][1] - acks[0][1]), 2)


def catch_up(poller, last_id, wal, stopped, timeout):
    """Wait for every definition of "caught up", then time each from the moment writes stopped."""
    def done(target_sample, source_sample):
        return (target_sample["max_id"] is not None and target_sample["max_id"] >= last_id
                and None not in (target_sample["received"], target_sample["reported"], source_sample["confirmed"])
                and min(target_sample["received"], target_sample["reported"], source_sample["confirmed"]) >= wal)

    poller.wait_until(done, timeout)

    def elapsed(samples, position):
        reached = first_reach(samples, position)
        return None if reached is None else round((reached - stopped) * 1000, 1)

    return {
        # Row arrival: what the Executor's barrier wait observes.
        "visible_ms": elapsed(poller.seen(), last_id),
        # The target has received the stopped position.
        "received_lsn_ms": elapsed(poller.positions("target", "received"), wal),
        # pg_stat_subscription.latest_end_lsn: the Executor's gate and receipt watermark.
        "reported_lsn_ms": elapsed(poller.positions("target", "reported"), wal),
        # pg_replication_slots.confirmed_flush_lsn: the source knows the target flushed.
        "confirmed_lsn_ms": elapsed(poller.positions("source", "confirmed"), wal),
    }


def row_counts(connection):
    return connection.execute("""
        SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM attachments), (SELECT max(id) FROM messages)""").fetchone()


def autovacuum_runs(connection):
    """A vacuum after bulk data writes many WAL bytes and no rows, so it must be attributable."""
    return connection.execute("""
        SELECT autovacuum_count FROM pg_stat_user_tables WHERE relname = 'messages'""").fetchone()[0]


def measure_steady(lab, source, target, seconds):
    sent_before = lab.tx_bytes("source")
    vacuums_before = (autovacuum_runs(source), autovacuum_runs(target))
    wal_before = source.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]
    poller = Poller(lab)
    poller.start()
    acks, latencies = write_load(source, seconds=seconds)
    stopped = acks[-1][1]
    wal = parse_lsn(source.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0])
    wire_mbit = (lab.tx_bytes("source") - sent_before) * 8 / seconds / 1e6
    window_end = time.monotonic()
    catchup = catch_up(poller, acks[-1][0], wal, stopped, timeout=30)
    time.sleep(STATS_FLUSH_SECONDS)
    lags, missing = row_lags(acks, poller.seen())
    window_source = [sample for sample in poller.source if sample["t"] <= window_end]
    window_target = [sample for sample in poller.target if sample["t"] <= window_end]
    gate = gate_lag_bytes(window_source, window_target)
    return {
        "writes": len(acks), "write_rate_per_s": achieved_rate(acks),
        "write_latency_ms": summarize(latencies),
        "visible_lag_ms": summarize(lags), "rows_never_seen": missing,
        "builtin_replay_lag_ms": summarize([s["replay_lag_ms"] for s in window_source if s["replay_lag_ms"] is not None]),
        "gate_lag_bytes": summarize(gate),
        "gate_samples_over_limit_pct": round(sum(lag > GATE_BYTES for lag in gate) / len(gate) * 100, 2),
        "autovacuum_runs_in_window": {"source": autovacuum_runs(source) - vacuums_before[0],
                                      "target": autovacuum_runs(target) - vacuums_before[1]},
        "wal_bytes_per_s": round((wal - parse_lsn(wal_before)) / seconds),
        "wire_mbit": round(wire_mbit, 3),
        "sample_interval_ms": summarize(intervals_ms([sample["t"] for sample in poller.target])),
    }, catchup


def measure_run(condition, repetition, *, rows, steady_seconds):
    with Lab(CONDITIONS[condition]) as lab, connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target:
        path = lab.check_path()
        prepare(source, target)
        data_bytes = seed(source, rows)
        copied = copy_initial_data(lab, source, target)
        copied_ok = row_counts(source) == row_counts(target)
        before = machine_load()
        steady, catchup = measure_steady(lab, source, target, steady_seconds)
        after = machine_load()
        final_source, final_target = row_counts(source), row_counts(target)
        return {"condition": condition, "repetition": repetition, "path_check": path,
                "data_bytes": data_bytes, "initial_copy": {**copied, "rows_match": copied_ok},
                "steady": steady, "catchup": catchup,
                "rows_match_after_writes": final_source == final_target,
                "machine": {"before_steady": before, "after_catchup": after}}


def summarize_condition(runs):
    def column(*path):
        values = []
        for run in runs:
            value = run
            for key in path:
                value = value[key]
            values.append(value)
        return condense(values)

    return {
        "repetitions": len(runs),
        "initial_copy_ms": column("initial_copy", "ms"),
        "steady_visible_lag_mean_ms": column("steady", "visible_lag_ms", "mean"),
        "steady_visible_lag_p95_ms": column("steady", "visible_lag_ms", "p95"),
        "steady_visible_lag_max_ms": column("steady", "visible_lag_ms", "max"),
        "catchup_visible_ms": column("catchup", "visible_ms"),
        "catchup_received_lsn_ms": column("catchup", "received_lsn_ms"),
        "catchup_reported_lsn_ms": column("catchup", "reported_lsn_ms"),
        "catchup_confirmed_lsn_ms": column("catchup", "confirmed_lsn_ms"),
        "gate_lag_bytes_max": column("steady", "gate_lag_bytes", "max"),
        "gate_samples_over_limit_pct": column("steady", "gate_samples_over_limit_pct"),
        "path_rtt_ms": column("path_check", "rtt_ms"),
        "path_throughput_mbit": column("path_check", "throughput_mbit"),
    }


def budget_view(results):
    """Worst measured catch-up plus the larger T13 comparison time; a slower link sets the gate case."""
    gate = results["validation"].get("20mbit-rtt30", {}).get("gate_backlog")
    return budget_scenarios(results["conditions"], gate, budget_ms=BUDGET_MS, compare_ms=COMPARE_MS[1], poll_ms=EXECUTOR_POLL_MS)


def scripts_sha256():
    return {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in sorted(HERE.glob("*.py"))}


def environment():
    image = json.loads(docker("image", "inspect", POSTGRES_IMAGE))[0]
    info = json.loads(docker("info", "--format", "{{json .}}"))
    return {"postgres_image": POSTGRES_IMAGE, "postgres_image_id": image["Id"],
            "db_container_limits": list(DB_LIMITS), "db_parameters": DB_PARAMETERS,
            "docker_vm": {"cpus": info["NCPU"], "memory_gib": round(info["MemTotal"] / 2 ** 30, 1), "kernel": info["KernelVersion"]},
            "host_clock": "single process, time.monotonic", "scripts_sha256": scripts_sha256(),
            "schema_sha256": {
                "source": hashlib.sha256(schema_text(SOURCE_SCHEMA).encode()).hexdigest(),
                "target": hashlib.sha256(schema_text(TARGET_SCHEMA, TARGET_SCHEMA_REF).encode()).hexdigest()}}


def load_results(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"schema": "t8-replication-lag/v1", "measured_on": time.strftime("%Y-%m-%d"),
            "assumed_not_measured": {
                "link_conditions": "netem values below are assumptions, not the actual Tailscale path",
                "compare_ms": list(COMPARE_MS), "compare_source": "T13 local measurement, source plus target"},
            "write_rate_per_s": WRITE_RATE, "body_chars": BODY_CHARS, "environment": environment(),
            "conditions": {}, "size_checks": {}, "validation": {}}


def save_results(path, results):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    temporary.replace(path)


def command_lab(args):
    results = load_results(args.out)
    rows = {table: count * args.size_factor for table, count in ROWS.items()}
    build_shaper()
    try:
        # Interleaved, so load from other sessions drifting over time cannot favour one condition.
        for repetition in range(1, args.reps + 1):
            for condition in args.conditions:
                section = "size_checks" if args.size_factor != 1 else "conditions"
                key = f"{condition}-x{args.size_factor}" if args.size_factor != 1 else condition
                entry = results[section].setdefault(key, {"shaping": CONDITIONS[condition], "rows": rows, "runs": []})
                if len(entry["runs"]) >= repetition:
                    continue
                run = measure_run(condition, repetition, rows=rows, steady_seconds=args.steady_seconds)
                entry["runs"].append(run)
                entry["summary"] = summarize_condition(entry["runs"])
                results["budget_view"] = budget_view(results)
                save_results(args.out, results)
                print(json.dumps({"condition": key, "repetition": repetition, "copy_ms": run["initial_copy"]["ms"],
                                  "lag_p95_ms": run["steady"]["visible_lag_ms"]["p95"], "catchup": run["catchup"]}), flush=True)
    finally:
        remove_shaper()


def transaction_ids(connection):
    return connection.execute("SELECT pg_snapshot_xmax(pg_current_snapshot())::text").fetchone()[0]


def quiet_autovacuum(connection):
    """Autovacuum spends transaction IDs too, so it must be gone before they can prove anything."""
    connection.execute("ALTER SYSTEM SET autovacuum = off")
    connection.execute("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 60
    while connection.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker'").fetchone()[0]:
        if time.monotonic() > deadline:
            raise TimeoutError("autovacuum worker did not finish")
        time.sleep(0.2)


def validate_read_only(lab, source, target, lab_p50_ms, write_seconds=10):
    """Under writes observe must agree with the lab; on a quiet pair it must report idle and write nothing."""
    dsns = (lab.dsn("source"), lab.dsn("target"))
    arguments = {"subscription": SUBSCRIPTION, "slot": SUBSCRIPTION, "interval": 0.02}
    def write():
        with connect(dsns[0]) as connection:
            write_load(connection, seconds=write_seconds)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    under_load = observe(*dsns, seconds=write_seconds - 1, **arguments)
    writer.join()
    time.sleep(2)
    quiet_autovacuum(source)
    quiet_autovacuum(target)
    before = {"source_xmax": transaction_ids(source), "target_xmax": transaction_ids(target),
              "source_rows": row_counts(source), "target_rows": row_counts(target)}
    quiet = observe(*dsns, seconds=5, **arguments)
    after = {"source_xmax": transaction_ids(source), "target_xmax": transaction_ids(target),
             "source_rows": row_counts(source), "target_rows": row_counts(target)}
    # The check itself must be able to fail: a real write has to move the same counter.
    source.execute(INSERT_MESSAGE, (uuid.uuid4(), "control"))
    control_moves = transaction_ids(source) != after["source_xmax"]
    session = connect_read_only(dsns[0])
    try:
        session.connection.execute(INSERT_MESSAGE, (uuid.uuid4(), "refused"))
        refused = False
    except psycopg.errors.ReadOnlySqlTransaction:
        refused = True
    finally:
        session.close()
    agrees = abs(under_load["row_lag_ms"]["p50"] - lab_p50_ms) <= 2 * under_load["sample_period_ms"]["p50"]
    return {"under_load": under_load, "quiet": quiet, "quiet_before": before, "quiet_after": after,
            "lab_row_lag_p50_ms": lab_p50_ms, "agrees_with_lab_within_two_sample_periods": agrees,
            "no_transaction_ids_consumed_while_quiet": before == after,
            "transaction_id_counter_moves_on_a_real_write": control_moves,
            "write_on_observe_session_refused": refused,
            "passed": refused and before == after and control_moves and quiet["source_idle"] and not under_load["source_idle"]
            and under_load["rows_not_seen_on_target"] == 0 and agrees}


def validate_condition(condition, pause_seconds=5):
    rows = {"messages": 1_900, "attachments": 100}
    with Lab(CONDITIONS[condition]) as lab, connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target:
        prepare(source, target)
        seed(source, rows)
        subscribe(lab, target)
        wait_ready(target)
        poller = Poller(lab)
        poller.start()
        baseline, _ = write_load(source, seconds=10)
        time.sleep(1)  # Let the last baseline row arrive, or the pause below counts against it.
        target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} DISABLE")
        paused, _ = write_load(source, seconds=pause_seconds)
        target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} ENABLE")
        resumed, _ = write_load(source, seconds=10)
        baseline_lag = summarize(row_lags(baseline, poller.seen())[0])
        paused_lag = summarize(row_lags(paused, poller.seen())[0])
        resumed_lag = summarize(row_lags(resumed, poller.seen())[0])
        # Past wal_retrieve_retry_interval, or the launcher delays the restart and blurs the next timing.
        time.sleep(6)

        # Stop writes behind a stopped subscription until the backlog reaches the Executor gate.
        target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} DISABLE")
        wal_start = parse_lsn(source.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0])

        def below_gate(_):
            return parse_lsn(source.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0]) - wal_start < GATE_BYTES

        backlog, _ = write_load(source, keep_going=below_gate)
        stopped = backlog[-1][1]
        wal = parse_lsn(source.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0])
        idle_seconds = 2
        time.sleep(idle_seconds)
        enabled = time.monotonic()
        target.execute(f"ALTER SUBSCRIPTION {SUBSCRIPTION} ENABLE")
        catchup = catch_up(poller, backlog[-1][0], wal, stopped, timeout=60)
        visible = first_reach(poller.seen(), backlog[-1][0])
        read_only = validate_read_only(lab, source, target, baseline_lag["p50"])
        # The read-only check writes one control row; compare only after it can have arrived.
        wait_for_row(target, row_counts(source)[2])
        checks = {
            "baseline_p95_below_20pct_of_pause": baseline_lag["p95"] < pause_seconds * 200,
            "paused_max_reaches_80pct_of_pause": paused_lag["max"] >= pause_seconds * 800,
            "lag_recovers_after_enable": resumed_lag["p50"] < pause_seconds * 200,
            "catchup_includes_stopped_subscription": catchup["visible_ms"] is not None and catchup["visible_ms"] >= idle_seconds * 1000,
            "read_only": read_only["passed"],
            "rows_match": row_counts(source) == row_counts(target),
        }
        return {"condition": condition, "pause_seconds": pause_seconds,
                "baseline_lag_ms": baseline_lag, "paused_lag_ms": paused_lag, "resumed_lag_ms": resumed_lag,
                "gate_backlog": {"wal_bytes": wal - wal_start, "rows": len(backlog),
                                 "catchup_from_write_stop_ms": catchup,
                                 "visible_after_enable_ms": None if visible is None else round((visible - enabled) * 1000, 1)},
                "read_only": read_only, "checks": checks, "passed": all(checks.values())}


def command_validate(args):
    results = load_results(args.out)
    build_shaper()
    try:
        for condition in args.conditions:
            outcome = validate_condition(condition)
            results["validation"][condition] = outcome
            results["validation_scripts_sha256"] = scripts_sha256()
            results["budget_view"] = budget_view(results)
            save_results(args.out, results)
            print(json.dumps({"condition": condition, "passed": outcome["passed"], "checks": outcome["checks"]}), flush=True)
    finally:
        remove_shaper()


def command_observe(args):
    source_dsn, target_dsn = dsns_from_environment()
    result = observe(source_dsn, target_dsn, subscription=args.subscription, slot=args.slot or args.subscription,
                     seconds=args.seconds, interval=args.interval)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    lab = commands.add_parser("lab", help="write to disposable local containers")
    lab.add_argument("--out", type=Path, required=True)
    lab.add_argument("--conditions", nargs="+", choices=list(CONDITIONS), default=list(CONDITIONS))
    lab.add_argument("--reps", type=int, default=5)
    lab.add_argument("--steady-seconds", type=int, default=60)
    lab.add_argument("--size-factor", type=int, default=1)
    lab.set_defaults(run=command_lab)
    validate = commands.add_parser("validate", help="check that the measurement responds")
    validate.add_argument("--out", type=Path, required=True)
    validate.add_argument("--conditions", nargs="+", choices=list(CONDITIONS), default=["unlimited"])
    validate.set_defaults(run=command_validate)
    read_only = commands.add_parser("observe", help="read-only sampling; DSNs from the environment")
    read_only.add_argument("--subscription", default=SUBSCRIPTION)
    read_only.add_argument("--slot", help="default: same as the subscription")
    read_only.add_argument("--seconds", type=int, default=60)
    read_only.add_argument("--interval", type=float, default=0.2)
    read_only.add_argument("--out", type=Path)
    read_only.set_defaults(run=command_observe)
    args = parser.parse_args()
    # Docker cleanup runs in finally blocks; turn a polite kill into an ordinary exit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.setswitchinterval(0.001)  # Keep the polling thread from waiting on the writer's interpreter slice.
    try:
        args.run(args)
    except psycopg.Error as error:
        # Class name only: a driver message can carry a host name taken from the DSN.
        raise SystemExit(f"database error: {type(error).__name__}") from None


if __name__ == "__main__":
    main()
