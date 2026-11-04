"""잠금 측정. 통계는 항상, 실제 잠금 시험은 TEST_DATABASE_URL이 있을 때만 돈다."""

import os
import threading
import time

import psycopg
import pytest

from bravo_loadgen.lock_probe import LockProbe, LockSample, summarize_lock_samples

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL이 없다")
KEY = 987_654_321


def sample(phase, wait, waiters=0, timed_out=False, elapsed=None):
    return LockSample(phase, wait, waiters, timed_out, wait if elapsed is None else elapsed)


def test_summary_separates_phases():
    samples = [sample("baseline", 1.0), sample("load", 10.0, 2), sample("load", 30.0, 3),
               sample("load", 5000.0, 9, True), sample("drain", 9000.0, 9)]
    s = summarize_lock_samples(samples, key=4310001, connection_errors=2)
    assert s["key"] == 4310001 and s["connection_errors"] == 2
    assert s["baseline"]["samples"] == 1 and s["baseline"]["wait_ms"]["max"] == 1.0
    # 부하가 끝난 뒤 표본은 "초당 50건일 때"에 넣지 않는다.
    assert s["load"]["samples"] == 3 and s["load"]["timed_out"] == 1
    assert s["load"]["wait_ms"]["max"] == 5000.0 and s["load"]["waiting_sessions_max"] == 9


def test_long_block_is_counted_for_the_probes_it_delayed():
    """표본은 앞 표본이 끝나야 잡힌다. 4초 막혀도 표본이 1개면 p95에 안 보이므로, 그동안 왔을 요청의 대기를 넣는다."""
    samples = [sample("load", 0.01) for _ in range(79)] + [sample("load", 4000.0)]
    wait = summarize_lock_samples(samples, key=1)["load"]["wait_ms"]
    assert wait["max"] == 4000.0
    assert wait["p95"] >= 1000.0, wait
    assert summarize_lock_samples(samples, key=1)["load"]["samples"] == 80


def test_stall_is_seen_from_the_client_when_the_db_clock_shows_nothing():
    """DB가 통째로 멈추면 DB 시계로는 잠금 대기가 0이다. 클라이언트가 기다린 시간으로 드러낸다."""
    s = summarize_lock_samples([sample("load", 0.01, elapsed=5000.0), sample("load", 0.01)], key=1)
    assert s["load"]["stalled"] == 1 and s["load"]["round_trip_ms"]["max"] == 5000.0


def test_no_samples_is_null_not_zero():
    s = summarize_lock_samples([], key=1)
    assert s["load"]["samples"] == 0 and s["load"]["wait_ms"] is None


def wait_for_samples(probe, count, seconds=5):
    deadline = time.monotonic() + seconds
    while len(probe.samples) < count and time.monotonic() < deadline:
        time.sleep(0.01)


@needs_db
def test_measures_a_held_lock_and_releases_it():
    with psycopg.connect(DSN) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (KEY,))
        probe = LockProbe(DSN, key=KEY)
        probe.start()
        time.sleep(0.3)
        holder.rollback()
        wait_for_samples(probe, 2)
        probe.stop()
    assert probe.samples[0].wait_ms >= 250  # 잠금을 쥔 동안 실제로 기다렸다
    assert probe.samples[-1].wait_ms < 50  # 풀린 뒤에는 바로 잡는다
    with psycopg.connect(DSN) as connection:
        got = connection.execute("SELECT pg_try_advisory_xact_lock(%s::bigint)", (KEY,)).fetchone()[0]
    assert got is True


@needs_db
def test_counts_waiting_sessions():
    with psycopg.connect(DSN) as holder, psycopg.connect(DSN, autocommit=True) as waiter:
        holder.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (KEY + 1,))
        waiter.execute("SET statement_timeout = 500")

        def wait_for_lock():
            try:
                waiter.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (KEY + 1,))
            except psycopg.errors.QueryCanceled:
                pass

        thread = threading.Thread(target=wait_for_lock)
        thread.start()
        time.sleep(0.2)
        probe = LockProbe(DSN, key=KEY + 1)
        probe.start()
        time.sleep(0.1)
        thread.join()
        holder.rollback()
        wait_for_samples(probe, 1)
        probe.stop()
    assert probe.samples[0].waiting_sessions == 1


@needs_db
def test_times_out_on_the_server_and_records_it(monkeypatch):
    import bravo_loadgen.lock_probe as module

    monkeypatch.setattr(module, "PROBE_STATEMENT_TIMEOUT_MS", 100)
    with psycopg.connect(DSN) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (KEY + 2,))
        probe = LockProbe(DSN, key=KEY + 2)
        probe.start()
        wait_for_samples(probe, 1)
        probe.stop()
        holder.rollback()
    assert probe.samples[0].timed_out is True and probe.samples[0].wait_ms == 100


@needs_db
def test_a_sample_still_blocked_at_stop_is_recorded_not_lost(monkeypatch):
    """끝날 때까지 막혀 있던 표본을 버리면 멈춘 구간이 값에서 사라진다."""
    import bravo_loadgen.lock_probe as module

    monkeypatch.setattr(module, "STOP_WAIT_S", 0.1)
    with psycopg.connect(DSN) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (KEY + 3,))
        probe = LockProbe(DSN, key=KEY + 3)
        probe.phase = "load"
        probe.start()
        time.sleep(0.4)
        probe.stop()
        holder.rollback()
    last = probe.samples[-1]
    assert last.phase == "load" and last.timed_out is True and last.elapsed_ms >= 400


def test_survives_connection_failure():
    probe = LockProbe("postgresql://u:p@127.0.0.1:1/none?connect_timeout=1")
    probe.start()
    time.sleep(0.1)
    probe.stop()
    assert probe.connection_errors >= 1 and probe.samples == []


def test_survives_a_malformed_dsn_without_raising():
    """부하가 끝난 뒤 터져 결과를 잃거나, 메시지로 비밀번호가 새면 안 된다."""
    probe = LockProbe("postgresql://chat:Sup3r%Secret@db/none")
    probe.start()
    time.sleep(0.1)
    probe.stop()
    assert probe.connection_errors >= 1


@needs_db
def test_does_not_hold_the_lock_between_samples():
    """측정기가 잠금을 쥔 채 쉬면 앱의 쓰기를 막아 재려는 값을 스스로 망친다."""
    probe = LockProbe(DSN, key=KEY + 4)
    probe.start()
    got = []
    with psycopg.connect(DSN) as other:
        for _ in range(40):
            got.append(other.execute("SELECT pg_try_advisory_xact_lock(%s::bigint)", (KEY + 4,)).fetchone()[0])
            other.rollback()
            time.sleep(0.02)
    probe.stop()
    assert len(probe.samples) >= 3  # 측정기가 실제로 도는 동안 확인했다
    assert sum(got) >= 36, f"40번 중 {sum(got)}번만 잠금을 잡았다"


@needs_db
def test_releases_the_lock_when_the_statement_ends():
    """결과를 받고 놓는 방식이면 측정기 쪽 왕복만큼 앱 쓰기를 막는다. 문장이 끝나면 바로 풀려야 한다."""
    probe = LockProbe(DSN, key=KEY + 5)
    with probe._connect() as connection, psycopg.connect(DSN) as other:
        probe._sample(connection)
        got = other.execute("SELECT pg_try_advisory_xact_lock(%s::bigint)", (KEY + 5,)).fetchone()[0]
    assert got is True
