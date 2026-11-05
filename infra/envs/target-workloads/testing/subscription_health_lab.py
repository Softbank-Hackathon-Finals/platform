#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""Break the link between two throwaway PostgreSQL containers and read the metrics as CNPG's exporter would.

Stand-ins, all assumed rather than measured from the real path: plain PostgreSQL instead of CNPG, a netem link
of 100 Mbps and RTT 30 ms, no TLS. The subscriptions use replication.py's parameters and the exporter query is
render.py's, run as a pg_monitor member in a read-only transaction, as CNPG does. It reads from the postgres
database, not chat, so the result does not depend on which database the exporter connects to.
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import psycopg
from lag_lab import CONDITIONS, Lab, build_shaper, machine_load, remove_shaper
from render import SUBSCRIPTION_QUERIES
from replication import names, runtime_state_sql, subscription
from replication_lag import connect, prepare, wait_ready, write_load
from replication_resume_lab import Writer, slot_row, stop_cleanly, wait_until
from subscription_metrics import condense, run_query

FORWARD = names("target")[2]
REVERSE = names("source")[2]
HOLD_SECONDS = 15  # After recovering it must stay on: the stop may come when the link returns.
OBSERVE_SECONDS = 60
SAMPLE_SECONDS = 0.25
CONFLICT_SECONDS = 30
EXPORTER_ROLE = "cnpg_metrics_exporter"
QUERY = SUBSCRIPTION_QUERIES["pg_subscription"]
# Failure name, seconds, percent of packets lost.
LINK_FAILURES = (("cut_8s", 8, 100), ("cut_20s", 20, 100), ("cut_40s", 40, 100), ("loss30_30s", 30, 30))


def with_options(parameters):
    return ", ".join(f"{key} = {value if value in ('true', 'false') else repr(value)}" for key, value in parameters.items())


def create_subscriptions(lab, source, target):
    """The rendered parameters on both sides. The reverse stays disabled with its own setting, as it does in reality."""
    for side, connection in (("target", target), ("source", source)):
        peer = "source" if side == "target" else "target"
        spec = subscription(side)["spec"]
        connection.execute(f"""CREATE SUBSCRIPTION {spec['name']}
            CONNECTION 'host={lab.wan[peer]['ip']} port=5432 dbname=chat user=chat_replicator connect_timeout=10'
            PUBLICATION {spec['publicationName']} WITH ({with_options(spec['parameters'])})""")


class Exporter:
    """The same session shape as CNPG's exporter: a non-superuser pg_monitor member with its search_path."""

    def __init__(self, lab, target):
        target.execute(f"CREATE ROLE {EXPORTER_ROLE} LOGIN")
        target.execute(f"GRANT pg_monitor TO {EXPORTER_ROLE}")
        self.connection = psycopg.connect(lab.dsn("target", user=EXPORTER_ROLE, dbname="postgres"), autocommit=True)

    def execute(self, sql):
        with self.connection.transaction():
            self.connection.execute("SET TRANSACTION READ ONLY")
            self.connection.execute("SET LOCAL search_path = pg_catalog, public, pg_temp")
            cursor = self.connection.execute(sql)
            return [column.name for column in cursor.description], cursor.fetchall()

    def scrape(self):
        return run_query(self.execute, QUERY)[FORWARD]


def subscription_settings(target, source):
    forward = target.execute("SELECT subenabled, subdisableonerr FROM pg_subscription WHERE subname = %s", (FORWARD,)).fetchone()
    reverse = source.execute("SELECT subenabled, subdisableonerr FROM pg_subscription WHERE subname = %s", (REVERSE,)).fetchone()
    return {"forward_enabled": forward[0], "forward_disable_on_error": forward[1], "reverse_enabled": reverse[0], "reverse_disable_on_error": reverse[1]}


def start_forward(source, target, disable_on_error):
    if target.execute("SELECT subenabled FROM pg_subscription WHERE subname = %s", (FORWARD,)).fetchone()[0]:
        stop_cleanly(source, target)
    else:
        wait_until(lambda: not slot_row(source)[0], 90)
    target.execute(f"ALTER SUBSCRIPTION {FORWARD} SET (disable_on_error = {'true' if disable_on_error else 'false'})")
    target.execute(runtime_state_sql("target", True))
    if not wait_until(lambda: slot_row(source)[0], 60):
        raise TimeoutError("worker did not attach")


def run_link_trial(lab, source, target, exporter, disable_on_error, failure, seconds, loss, repetition, *, writes=True):
    start_forward(source, target, disable_on_error)
    writer = Writer(lab) if writes else None
    if writer:
        writer.start()
    time.sleep(3)
    before = exporter.scrape()
    samples, stopped_after, flowing_after, held_until = [], None, None, None
    cut = time.monotonic()
    lab.link_down(loss)

    def sample():
        nonlocal stopped_after
        now = exporter.scrape()
        samples.append({"t": round(time.monotonic() - cut, 2), **now, "slot_active": slot_row(source)[0]})
        if not now["enabled"] and stopped_after is None:
            stopped_after = samples[-1]["t"]

    while time.monotonic() < cut + seconds:
        sample()
        time.sleep(SAMPLE_SECONDS)
    lab.link_up()
    healed = time.monotonic()
    newest = source.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
    position = source.execute("SELECT pg_current_wal_flush_lsn()").fetchone()[0]
    while time.monotonic() < healed + OBSERVE_SECONDS and stopped_after is None:
        sample()
        arrived = (target.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0] >= newest if writes
                   else source.execute("SELECT confirmed_flush_lsn >= %s FROM pg_replication_slots WHERE slot_name = %s", (position, FORWARD)).fetchone()[0])
        if samples[-1]["slot_active"] and arrived and flowing_after is None:
            flowing_after = round(time.monotonic() - healed, 2)
            held_until = time.monotonic() + HOLD_SECONDS
        if flowing_after is not None and time.monotonic() >= held_until:
            break
        time.sleep(SAMPLE_SECONDS)
    if writer:
        writer.stop()
    after = exporter.scrape()
    return {"disable_on_error": disable_on_error, "failure": failure, "writes": writes, "repetition": repetition,
            "outcome": "stopped" if stopped_after is not None else "recovered" if flowing_after is not None else "no_recovery_seen",
            "stopped_after_s": stopped_after, "rows_flowing_after_heal_s": None if stopped_after is not None else flowing_after,
            "enabled_lowest": min(item["enabled"] for item in samples), "workers_lowest": min(item["apply_workers"] for item in samples),
            "slot_active_samples": sum(item["slot_active"] for item in samples), "samples": len(samples),
            "apply_errors_added": after["apply_error_count"] - before["apply_error_count"], "before": before, "after": after,
            "settings_after": subscription_settings(target, source)}


def run_conflict_trial(lab, source, target, exporter, disable_on_error, repetition):
    """A row already on the target, as a bypassing write would leave it. The retry is what is being counted."""
    start_forward(source, target, disable_on_error)
    time.sleep(2)
    before = exporter.scrape()
    # From the sequence, so that a later ordinary insert can never run into this id.
    row_id = source.execute("SELECT nextval(pg_get_serial_sequence('public.messages', 'id'))").fetchone()[0]
    target.execute("INSERT INTO messages (id, idempotency_key, author, body) VALUES (%s, %s, 'conflict', 'x')", (row_id, uuid.uuid4()))
    source.execute("INSERT INTO messages (id, idempotency_key, author, body) VALUES (%s, %s, 'conflict', 'y')", (row_id, uuid.uuid4()))
    samples, began = [], time.monotonic()
    while time.monotonic() - began < CONFLICT_SECONDS:
        samples.append({**exporter.scrape(), "slot_active": slot_row(source)[0]})
        time.sleep(SAMPLE_SECONDS)
    after = exporter.scrape()
    target.execute("DELETE FROM messages WHERE id = %s AND body = 'x'", (row_id,))
    healed_alone = wait_until(lambda: target.execute("SELECT body FROM messages WHERE id = %s", (row_id,)).fetchone() == ("y",), 20) if after["enabled"] else False
    return {"disable_on_error": disable_on_error, "failure": "conflict", "repetition": repetition, "seconds": CONFLICT_SECONDS,
            "outcome": "stopped" if not after["enabled"] else "recovered" if healed_alone else "no_recovery_seen",
            "stopped_after_s": None, "rows_flowing_after_heal_s": None, "enabled_lowest": min(item["enabled"] for item in samples),
            "workers_lowest": min(item["apply_workers"] for item in samples),
            "slot_active_samples": sum(item["slot_active"] for item in samples), "samples": len(samples),
            "apply_errors_added": after["apply_error_count"] - before["apply_error_count"], "healed_after_removing_the_row": healed_alone,
            "settings_after": subscription_settings(target, source)}


def run_live_alter(lab, source, target, exporter, repetition):
    """The documented procedure on a worker that is already streaming: off, a cut survives, back on, a cut stops it."""
    start_forward(source, target, True)
    writer = Writer(lab)
    writer.start()
    time.sleep(3)
    worker = "SELECT pid FROM pg_stat_subscription WHERE subname = %s AND worker_type = 'apply'"
    pid_before = target.execute(worker, (FORWARD,)).fetchone()[0]
    target.execute(f'ALTER SUBSCRIPTION "{FORWARD}" SET (disable_on_error = false)')
    time.sleep(1)
    pid_after_off = target.execute(worker, (FORWARD,)).fetchone()[0]
    settings_off = subscription_settings(target, source)
    errors = exporter.scrape()["apply_error_count"]
    lab.link_down(100)
    time.sleep(8)
    lab.link_up()
    newest = source.execute("SELECT max(id) FROM messages").fetchone()[0]
    recovered = wait_until(lambda: target.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0] >= newest, 60)
    enabled_after_cut = exporter.scrape()["enabled"]
    errors_after_cut = exporter.scrape()["apply_error_count"] - errors
    time.sleep(1)
    pid_before_rollback = target.execute(worker, (FORWARD,)).fetchone()[0]
    target.execute(f'ALTER SUBSCRIPTION "{FORWARD}" SET (disable_on_error = true)')
    time.sleep(1)
    pid_after_rollback = target.execute(worker, (FORWARD,)).fetchone()[0]
    lab.link_down(100)
    time.sleep(8)
    lab.link_up()
    stopped_again = wait_until(lambda: not exporter.scrape()["enabled"], 20)
    writer.stop()
    return {"repetition": repetition, "worker_kept_on_off": pid_before == pid_after_off, "settings_off": settings_off,
            "recovered_with_off": recovered, "enabled_after_cut": enabled_after_cut, "errors_added_with_off": errors_after_cut,
            "worker_kept_on_rollback": pid_before_rollback == pid_after_rollback, "stopped_after_rollback": stopped_again,
            "reverse_after": subscription_settings(target, source)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    results = {"stand_ins": ["plain PostgreSQL 18.4 containers instead of CNPG", "netem 100 Mbps RTT 30 ms is assumed", "no TLS", "no seed data"],
               "exporter_session": "non-superuser pg_monitor member on the postgres database, read-only transaction, CNPG's search_path",
               "machine": {"before": machine_load()}, "link_trials": [], "idle_trials": [], "conflict_trials": [], "live_alter": []}
    out = Path(args.out)
    build_shaper()
    try:
        with Lab(CONDITIONS["100mbit-rtt30"]) as lab, connect(lab.dsn("source")) as source, connect(lab.dsn("target")) as target:
            prepare(source, target)
            write_load(source, seconds=1)
            create_subscriptions(lab, source, target)
            wait_ready(target)
            exporter = Exporter(lab, target)
            results["columns_match_query"] = run_query(exporter.execute, QUERY) is not None
            results["postgres"] = target.execute("SHOW server_version").fetchone()[0]
            results["timeouts"] = {"wal_receiver_timeout": target.execute("SHOW wal_receiver_timeout").fetchone()[0],
                                   "wal_sender_timeout": source.execute("SHOW wal_sender_timeout").fetchone()[0]}
            for repetition in range(args.repetitions):
                for disable_on_error in (True, False):
                    for failure, seconds, loss in LINK_FAILURES:
                        results["link_trials"].append(run_link_trial(lab, source, target, exporter, disable_on_error, failure, seconds, loss, repetition))
                        print(json.dumps({key: results["link_trials"][-1][key] for key in ("disable_on_error", "failure", "repetition", "outcome", "stopped_after_s", "rows_flowing_after_heal_s", "apply_errors_added", "workers_lowest")}), flush=True)
                        out.write_text(json.dumps(results, indent=2) + "\n")
                    results["idle_trials"].append(run_link_trial(lab, source, target, exporter, disable_on_error, "idle_cut_8s", 8, 100, repetition, writes=False))
                    results["conflict_trials"].append(run_conflict_trial(lab, source, target, exporter, disable_on_error, repetition))
                results["live_alter"].append(run_live_alter(lab, source, target, exporter, repetition))
                out.write_text(json.dumps(results, indent=2) + "\n")
            results["summary"] = condense(results["link_trials"] + results["idle_trials"] + results["conflict_trials"])
    finally:
        remove_shaper()
    results["machine"]["after"] = machine_load()
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["summary"], indent=2))


if __name__ == "__main__":
    main()
