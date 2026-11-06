#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""Stall the replication link for a few seconds and compare 5 s with 30 s replication timeouts.

Stand-ins, all assumed rather than measured from the real path: plain PostgreSQL 18.4 instead of CNPG, a netem
link of 100 Mbps and RTT 90 ms (the relayed tailnet path measured 67 to 107 ms), no TLS. A stall adds one-way
delay in both directions for ten seconds and drops nothing; the real stall's shape is unknown. Subscriptions use
replication.py's rendered parameters. The timeouts change by ALTER SYSTEM and a reload while the worker keeps
running, the way t8-timeout-apply.sh would change the real clusters.
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from lag_lab import Lab, build_shaper, machine_load, remove_shaper
from lag_metrics import parse_log_events, summarize_stall_trials
from replication import names, runtime_state_sql
from replication_lag import connect, prepare, wait_ready, write_load
from replication_resume_lab import LOG_PARAMETERS, Writer, slot_row, wait_until
from subscription_health_lab import create_subscriptions, subscription_settings

FORWARD = names("target")[2]
RELAY = {"mbit": 100, "rtt_ms": 90}
TIMEOUTS = (5, 30)
STALL_SECONDS = 10
OBSERVE_SECONDS = 120  # Long enough for a 30 s timeout, the reconnect after it and the catch-up.
HOLD_SECONDS = 10  # A timeout can still fire after the link is back; keep watching a little longer.
SAMPLE_SECONDS = 0.25
# Name, kind, extra one-way delay in ms or cut seconds, whether rows are being written, forward disable_on_error.
CONDITIONS = (
    ("stall_2s", "stall", 2000, True, False),
    ("stall_4s", "stall", 4000, True, False),
    ("stall_6s", "stall", 6000, True, False),
    ("idle_stall_4s", "stall", 4000, False, False),
    ("stall_4s_disable_on_error", "stall", 4000, True, True),
    ("cut_20s", "cut", 20, True, False),
)
WORKER = "SELECT pid FROM pg_stat_subscription WHERE subname = %s AND worker_type = 'apply'"
SENDER = "SELECT pid FROM pg_stat_replication WHERE application_name = %s"


def effective_timeouts(connection):
    return connection.execute("""SELECT (SELECT setting::int / 1000 FROM pg_settings WHERE name = 'wal_sender_timeout'),
                                        (SELECT setting::int / 1000 FROM pg_settings WHERE name = 'wal_receiver_timeout')""").fetchone()


def pids(source, target):
    worker = target.execute(WORKER, (FORWARD,)).fetchone()
    sender = source.execute(SENDER, (FORWARD,)).fetchone()
    return {"apply_worker": worker and worker[0], "walsender": sender and sender[0]}


def apply_errors(target):
    return target.execute("""SELECT e.apply_error_count FROM pg_stat_subscription_stats AS e
        JOIN pg_subscription AS s ON s.oid = e.subid WHERE s.subname = %s""", (FORWARD,)).fetchone()[0]


def is_enabled(target):
    return target.execute("SELECT subenabled FROM pg_subscription WHERE subname = %s", (FORWARD,)).fetchone()[0]


def newest(connection):
    return connection.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]


def set_timeouts(source, target, seconds):
    """Both values on both sides, then a reload. Returns whether the running processes stayed the same."""
    before = pids(source, target)
    for connection in (source, target):
        connection.execute(f"ALTER SYSTEM SET wal_sender_timeout = '{int(seconds)}s'")
        connection.execute(f"ALTER SYSTEM SET wal_receiver_timeout = '{int(seconds)}s'")
        connection.execute("SELECT pg_reload_conf()")
        # Trust the server, not the statement: a higher-priority setting would ignore it without an error.
        if not wait_until(lambda connection=connection: effective_timeouts(connection) == (seconds, seconds), 10):
            raise RuntimeError("timeout settings did not take effect")
    pending = [connection.execute("SELECT count(*) FROM pg_settings WHERE pending_restart").fetchone()[0] for connection in (source, target)]
    time.sleep(1)
    return {"timeout_s": seconds, "pids_kept": pids(source, target) == before, "pending_restart": pending,
            "effective": {"source": list(effective_timeouts(source)), "target": list(effective_timeouts(target))}}


def ensure_forward(source, target, disable_on_error):
    """Running, caught up and with the condition's disable_on_error, whatever the last trial left behind."""
    target.execute(f"ALTER SUBSCRIPTION {FORWARD} SET (disable_on_error = {'true' if disable_on_error else 'false'})")
    if not is_enabled(target):
        wait_until(lambda: not slot_row(source, FORWARD)[0], 90)
        target.execute(runtime_state_sql("target", True))
    if not wait_until(lambda: slot_row(source, FORWARD)[0] and pids(source, target)["apply_worker"]
                      and newest(target) >= newest(source), 90):
        raise TimeoutError("forward did not catch up before the trial")


def run_trial(lab, source, target, timeout_s, condition, repetition):
    name, kind, amount, writes, disable_on_error = condition
    ensure_forward(source, target, disable_on_error)
    writer = Writer(lab) if writes else None
    if writer:
        writer.start()
    time.sleep(3)
    errors_before, pids_before = apply_errors(target), pids(source, target)
    lines_before = {side: len(lab.log_lines(side)) for side in ("source", "target")}
    began = time.monotonic()
    lab.reshape(extra_delay_ms=amount) if kind == "stall" else lab.reshape(loss_pct=100)
    disabled_after, worker_gone_samples = None, 0

    def watch(until):
        nonlocal disabled_after, worker_gone_samples
        while time.monotonic() < until and disabled_after is None:
            if not is_enabled(target):
                disabled_after = round(time.monotonic() - began, 2)
            worker_gone_samples += pids(source, target)["apply_worker"] is None
            time.sleep(SAMPLE_SECONDS)

    watch(began + (STALL_SECONDS if kind == "stall" else amount))
    lab.reshape()
    healed = time.monotonic()
    if not writes:
        # Nothing else moves on an idle link; one row shows when the stream carries data again.
        source.execute("INSERT INTO messages (idempotency_key, author, body) VALUES (gen_random_uuid(), 'timeout-probe', 'x')")
    wanted, flowing_after = newest(source), None
    while time.monotonic() < healed + OBSERVE_SECONDS and disabled_after is None:
        if newest(target) >= wanted:
            flowing_after = round(time.monotonic() - healed, 2)
            break
        watch(time.monotonic() + SAMPLE_SECONDS)
    watch(time.monotonic() + HOLD_SECONDS)
    acked = [row_id for row_id, _ in writer.stop()] if writer else []
    # Every acknowledged row must arrive once the subscription runs again; a disabled one is turned back on first.
    ensure_forward(source, target, disable_on_error)
    arrived = {row[0] for row in target.execute("SELECT id FROM messages WHERE id = ANY(%s)", (acked,)).fetchall()}
    missing = len(set(acked) - arrived)
    pids_after = pids(source, target)
    target_events = parse_log_events(lab.log_lines("target")[lines_before["target"]:])
    source_events = parse_log_events(lab.log_lines("source")[lines_before["source"]:])
    ended = (apply_errors(target) > errors_before or pids_after != pids_before
             or any(item["event"] in ("receiver_timeout", "sender_timeout", "stream_lost") for item in target_events + source_events))
    return {"timeout_s": timeout_s, "condition": name, "kind": kind, "amount": amount, "writes": writes,
            "disable_on_error": disable_on_error, "repetition": repetition,
            "outcome": "disabled" if disabled_after is not None else "recovered" if flowing_after is not None else "no_recovery_seen",
            "disabled_after_s": disabled_after, "rows_flowing_after_heal_s": None if disabled_after is not None else flowing_after,
            "connection_ended": ended, "apply_errors_added": apply_errors(target) - errors_before,
            "worker_gone_samples": worker_gone_samples, "pids_replaced": {key: pids_after[key] != pids_before[key] for key in pids_before},
            "rows_written": len(acked), "rows_missing": missing,
            "target_log_events": target_events, "source_log_events": source_events,
            "settings_after": subscription_settings(target, source), "machine": machine_load()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    out = Path(args.out)
    results = {"stand_ins": ["plain PostgreSQL 18.4 containers instead of CNPG", "netem 100 Mbps RTT 90 ms is assumed",
                             f"a stall is extra one-way delay in both directions for {STALL_SECONDS} s; nothing is dropped",
                             "no TLS", "no seed data"],
               "machine": {"before": machine_load()}, "reloads": [], "trials": []}
    build_shaper()
    try:
        settings = {**LOG_PARAMETERS, "wal_sender_timeout": None, "wal_receiver_timeout": None}
        with Lab(RELAY, parameters=settings) as lab, connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target:
            lab.reshape()  # The deep queue from the first trial on, so that every trial runs on the same rule.
            results["path"] = lab.check_path()
            prepare(source, target)
            write_load(source, seconds=1)
            create_subscriptions(lab, source, target)
            wait_ready(target)
            results["postgres"] = target.execute("SHOW server_version").fetchone()[0]
            results["receiver_status_interval"] = target.execute("SHOW wal_receiver_status_interval").fetchone()[0]
            for repetition in range(args.repetitions):
                for timeout_s in TIMEOUTS:
                    results["reloads"].append({"repetition": repetition, **set_timeouts(source, target, timeout_s)})
                    for condition in CONDITIONS:
                        trial = run_trial(lab, source, target, timeout_s, condition, repetition)
                        results["trials"].append(trial)
                        print(json.dumps({key: trial[key] for key in ("timeout_s", "condition", "repetition", "outcome", "connection_ended",
                                                                       "apply_errors_added", "rows_flowing_after_heal_s", "rows_missing")}), flush=True)
                        results["summary"] = summarize_stall_trials(results["trials"])
                        out.write_text(json.dumps(results, indent=1) + "\n")
    finally:
        remove_shaper()
    results["machine"]["after"] = machine_load()
    out.write_text(json.dumps(results, indent=1) + "\n")
    print(json.dumps(results["summary"], indent=1))


if __name__ == "__main__":
    main()
