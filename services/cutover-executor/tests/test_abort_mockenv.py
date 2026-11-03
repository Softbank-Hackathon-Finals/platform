"""전환 확정 전 실패 → ABORT_PRE_WRITE (spec 커밋 6, 완성 기준 1 · 3).

주입: 긴 트랜잭션(사전 확인에서 거부), PAUSE deadline 초과, barrier 미도착, 6단계 구독 변경 뒤 실패.
끝나면 출발지 쓰기가 다시 되고 목적지로 복제되며, 목적지는 쓰기를 거부해야 한다.
"""

import json
import time
import urllib.request
import uuid

import psycopg
import pytest

from bravo_cutover.contract import Status
from bravo_cutover.remote.db import DbControl, WriteRefusal, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin
from bravo_cutover.runner import Cutover
from tests.mockenv_support import WriterWatch, mock_config, needs_mockenv, reset
from tests.test_cutover_mockenv import start

pytestmark = needs_mockenv


@pytest.fixture
def sides():
    config = mock_config()
    src, dst = config.sites["env-aws"], config.sites["env-onprem"]
    reset(src, dst)
    yield src, dst
    reset(src, dst)


def run_with(store, src, dst, hooks, **settings):
    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        Cutover(store, mock_config(**settings), hooks=hooks).run(execution_id)
    watch.assert_safe()
    return store.get(execution_id)


def assert_source_is_writer_again(src, dst):
    # 앱 경로로 출발지에 쓰고, 정방향 복제로 목적지에 도착한다
    key = uuid.uuid4()
    req = urllib.request.Request(
        "http://localhost:30480/api/messages", method="POST",
        data=json.dumps({"idempotency_key": str(key), "author": "test", "body": "after abort"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.status == 201
    ddb, deadline = DbControl(dst.db_control_dsn), time.monotonic() + 10
    while ddb._one("SELECT count(*) FROM messages WHERE idempotency_key = %s", (key,)) != 1:
        assert time.monotonic() < deadline, "source write did not replicate to target"
        time.sleep(0.2)
    assert try_app_write(dst.db_app_dsn, "after-abort") == WriteRefusal.REFUSED
    kube = Kube(src.kube_context)
    for site in (src, dst):
        state = PgBouncerAdmin(site.pgbouncer_admin_dsn, site.pgbouncer_database).state()
        assert (state.host, state.paused) == (src.db_host, False)
        assert not any(WriteBlock(kube, site.namespace, site.app_selector).get().values())
    assert DbControl(src.db_control_dsn).subscription(src.subscription).enabled is False


def failed_step(execution):
    return [s for s in execution.steps if s["status"] in ("failed", "unknown")][0]["step"]


def test_long_transaction_is_rejected_before_pause(store, sides):
    src, dst = sides
    tx = psycopg.connect(src.pgbouncer_app_dsn)

    def open_long_transaction(_run):
        tx.execute("SELECT 1")
        time.sleep(0.4)

    try:
        execution = run_with(store, src, dst, {"before:pause": open_long_transaction})
    finally:
        tx.rollback()
        tx.close()
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert failed_step(execution) == "pause" and "long transaction" in execution.result["error"]
    assert_source_is_writer_again(src, dst)


def test_pause_deadline_exceeded(store, sides):
    """긴 트랜잭션 검사 직후에 열린 트랜잭션이 PAUSE를 붙잡는다 → deadline 초과 → 쓰기 전 실패."""
    src, dst = sides
    tx = psycopg.connect(src.pgbouncer_app_dsn)
    try:
        execution = run_with(store, src, dst, {"before:pause": lambda _run: tx.execute("SELECT 1")},
                             pause_timeout_s=1, long_transaction_ms=60_000)
    finally:
        tx.rollback()
        tx.close()
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert failed_step(execution) == "pause" and "did not finish" in execution.result["error"]
    assert_source_is_writer_again(src, dst)


def test_barrier_does_not_arrive(store, sides):
    src, dst = sides
    ddb = DbControl(dst.db_control_dsn)
    stop_forward = lambda _run: ddb.set_subscription(dst.subscription, False, dst.subscription_owner)  # noqa: E731
    execution = run_with(store, src, dst, {"before:compare": stop_forward}, barrier_timeout_s=1.5)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert failed_step(execution) == "compare" and "barrier" in execution.result["error"]
    assert "forward_subscription_enabled" in execution.result["restored"]
    assert_source_is_writer_again(src, dst)


def test_failure_after_subscription_change_restores_replication(store, sides):
    """6단계에서 구독을 바꾼 뒤 확정 기록 전에 실패하면 구독까지 되돌린다."""
    src, dst = sides

    def crash(_run):
        raise RuntimeError("injected after subscription change")

    execution = run_with(store, src, dst, {"commit:subscriptions_changed": crash})
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.stage6_entered_at is not None and execution.committed_at is None
    assert {"reverse_subscription_disabled", "forward_subscription_enabled"} <= set(execution.result["restored"])
    assert_source_is_writer_again(src, dst)


def test_integrity_mismatch(store, sides):
    """목적지에만 있는 행이 생기면 비교가 MISMATCH를 내고 쓰기 전에 멈춘다."""
    import subprocess

    src, dst = sides
    key = uuid.uuid4()

    def psql(sql):
        subprocess.run(["kubectl", "--context", dst.kube_context, "exec", "-i", "-n", dst.namespace, "deploy/postgres",
                        "--", "psql", "-U", "postgres", "-d", "chat", "-v", "ON_ERROR_STOP=1"],
                       input=sql, text=True, check=True, capture_output=True)

    # 목적지에서 넣고 지운 기록도 다음 컷오버 때 역방향 복제로 출발지에 간다. 출발지 id와 겹치지 않게 큰 id를 쓴다.
    planted_id = 10**12 + uuid.uuid4().int % 10**9
    plant = lambda _run: psql(  # noqa: E731
        f"INSERT INTO messages (id, idempotency_key, author, body) VALUES ({planted_id}, '{key}', 'x', 'only on target');")
    try:
        execution = run_with(store, src, dst, {"before:compare": plant})
    finally:
        psql(f"DELETE FROM messages WHERE idempotency_key = '{key}';")
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert failed_step(execution) == "compare" and "MISMATCH" in execution.result["error"]
    assert_source_is_writer_again(src, dst)
