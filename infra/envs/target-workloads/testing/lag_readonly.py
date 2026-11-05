"""Read-only replication status: the only code path allowed near an actual database.

Writes are refused three times over: only the fixed statements below can be named, every
transaction opens READ ONLY, and a session the server does not report as read-only is closed.
DSNs come from the environment and are never written to output or error messages.
"""
import os
import threading
import time

from lag_metrics import analyze_observation, intervals_ms, parse_lsn, summarize

SOURCE_DSN_ENV = "T8_LAG_SOURCE_DSN"
TARGET_DSN_ENV = "T8_LAG_TARGET_DSN"
RTT_EVERY_SAMPLES = 10
READ_ONLY_CHECK = "SELECT current_setting('transaction_read_only')"
STATEMENTS = {
    "ping": "SELECT 1",
    # pg_stat_replication positions are NULL without pg_read_all_stats; the slot is world-readable.
    "source_status": """
        SELECT (SELECT max(id) FROM public.messages), pg_current_wal_lsn()::text,
               s.confirmed_flush_lsn::text, r.sent_lsn::text, r.replay_lsn::text,
               extract(epoch FROM r.replay_lag) * 1000
        FROM (SELECT 1) AS one
        LEFT JOIN pg_replication_slots AS s ON s.slot_name = %(slot)s
        LEFT JOIN pg_stat_replication AS r ON r.pid = s.active_pid""",
    # relid IS NULL selects the apply worker; the same filter as the Executor's lag gate.
    "target_status": """
        SELECT (SELECT max(id) FROM public.messages), w.received_lsn::text, w.latest_end_lsn::text
        FROM pg_stat_subscription AS w
        WHERE w.subname = %(subscription)s AND w.relid IS NULL""",
}


class NotReadOnly(RuntimeError):
    pass


class ReadOnlySession:
    """Runs only named statements, each in its own READ ONLY transaction."""

    def __init__(self, connection, statements=None):
        self.connection = connection
        self.statements = STATEMENTS if statements is None else statements
        connection.autocommit = False
        connection.read_only = True
        value = connection.execute(READ_ONLY_CHECK).fetchone()[0]
        connection.rollback()
        if value != "on":
            connection.close()
            raise NotReadOnly("server did not confirm a read-only transaction")

    def query(self, name, params=None):
        # BEGIN READ ONLY, the statement and ROLLBACK share one network round trip. Three separate
        # trips would make the sampling period, and so the resolution, three times coarser.
        with self.connection.pipeline():
            cursor = self.connection.execute(self.statements[name], params)
            self.connection.rollback()
        return cursor.fetchone()

    def round_trip_ms(self):
        """One network round trip. The first statement also pays for BEGIN, so only the second counts."""
        self.connection.execute(STATEMENTS["ping"]).fetchone()
        began = time.monotonic()
        self.connection.execute(STATEMENTS["ping"]).fetchone()
        elapsed = (time.monotonic() - began) * 1000
        self.connection.rollback()
        return elapsed

    def close(self):
        self.connection.close()


def dsns_from_environment():
    missing = [name for name in (SOURCE_DSN_ENV, TARGET_DSN_ENV) if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"missing environment variable(s): {', '.join(missing)}")
    return os.environ[SOURCE_DSN_ENV], os.environ[TARGET_DSN_ENV]


def read_source(query, slot):
    row = query("source_status", {"slot": slot})
    return {"max_id": row[0], "wal": parse_lsn(row[1]), "confirmed": parse_lsn(row[2]),
            "sent": parse_lsn(row[3]), "replay": parse_lsn(row[4]),
            "replay_lag_ms": None if row[5] is None else float(row[5])}


def read_target(query, subscription):
    row = query("target_status", {"subscription": subscription})
    if row is None:
        raise LookupError("subscription not found on target")
    return {"max_id": row[0], "received": parse_lsn(row[1]), "reported": parse_lsn(row[2])}


def connect_read_only(dsn):
    import psycopg  # Imported here so the arithmetic and its tests need no driver.

    # The options are a second guard for direct connections; a pooler may ignore them, which is
    # why ReadOnlySession does not rely on them.
    return ReadOnlySession(psycopg.connect(
        dsn, connect_timeout=10, application_name="t8_lag_observe",
        options="-c default_transaction_read_only=on -c statement_timeout=2000",
    ))


def _sample(session, read, samples, round_trips, stop, interval):
    while not stop.is_set():
        sample = read(session.query)
        sample["t"] = time.monotonic()
        samples.append(sample)
        if len(samples) % RTT_EVERY_SAMPLES == 1:
            round_trips.append(session.round_trip_ms())
        stop.wait(interval)


def observe(source_dsn, target_dsn, *, subscription, slot, seconds, interval, tail_seconds=2):
    """Sample both sides on this process's clock. Cannot stop writes, so it reports no catch-up time.

    A row's lag is only known to within one sampling period, reported as sample_period_ms.
    """
    source_session = connect_read_only(source_dsn)
    target_session = connect_read_only(target_dsn)
    source, target, source_rtt, target_rtt = [], [], [], []
    source_stop, target_stop, errors = threading.Event(), threading.Event(), []

    def guarded(*args):
        try:
            _sample(*args)
        except Exception as error:  # noqa: BLE001 - Reported as a class name only, so no DSN can leak.
            errors.append(type(error).__name__)
            source_stop.set()
            target_stop.set()

    threads = [
        threading.Thread(target=guarded, args=(source_session, lambda q: read_source(q, slot), source, source_rtt, source_stop, interval)),
        threading.Thread(target=guarded, args=(target_session, lambda q: read_target(q, subscription), target, target_rtt, target_stop, interval)),
    ]
    try:
        for thread in threads:
            thread.start()
        source_stop.wait(seconds)
        source_stop.set()
        # Rows the source showed just before the end still get time to appear on the target.
        target_stop.wait(tail_seconds)
    finally:
        source_stop.set()
        target_stop.set()
        for thread in threads:
            thread.join()
        source_session.close()
        target_session.close()
    if errors:
        raise RuntimeError(f"observation failed: {', '.join(sorted(set(errors)))}")
    return {"mode": "read-only-observe", "seconds": seconds, "interval_s": interval,
            "samples": {"source": len(source), "target": len(target)},
            "sample_period_ms": summarize(intervals_ms([sample["t"] for sample in target])),
            "round_trip_ms": {"source": summarize(source_rtt), "target": summarize(target_rtt)},
            **analyze_observation(source, target)}
