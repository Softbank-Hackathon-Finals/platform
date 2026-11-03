"""원격 조작 어댑터를 모의 환경(k3d)에서 시험한다: 조작 후 다시 읽어 상태를 확인한다 (spec 커밋 4).

testing/.state/sites.json이 있을 때만 돈다(python3 testing/mockenv.py up). CI에서는 건너뛴다.
시험마다 바꾼 상태를 원래대로 돌려놓는다.
"""

import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from bravo_cutover.remote.db import DbControl, WriteRefusal, app_write, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PauseTimeout, PgBouncerAdmin, PgBouncerConfig
from bravo_cutover.sites import load_config

SITES_FILE = Path(__file__).resolve().parents[1] / "testing" / ".state" / "sites.json"
pytestmark = pytest.mark.skipif(not SITES_FILE.exists(), reason="모의 환경 없음 (testing/mockenv.py up)")


@pytest.fixture(scope="module")
def sites():
    s = load_config(SITES_FILE).sites
    return s["env-aws"], s["env-onprem"]


@pytest.fixture(scope="module")
def kube(sites):
    return Kube(sites[0].kube_context)


def admin(site):
    return PgBouncerAdmin(site.pgbouncer_admin_dsn, site.pgbouncer_database)


def test_write_block_on_every_app_pod_and_back(sites, kube):
    for site in sites:
        block = WriteBlock(kube, site.namespace, site.app_selector)
        pods = block.pods()
        assert len(pods) == 2
        try:
            assert block.set(True) == dict.fromkeys(pods, True)
            assert block.get() == dict.fromkeys(pods, True)
        finally:
            block.set(False)
        assert block.get() == dict.fromkeys(pods, False)


def test_pause_resume_reads_back(sites):
    src = admin(sites[0])
    try:
        src.pause(timeout_s=5)
        assert src.state().paused
    finally:
        src.resume()
    assert not src.state().paused
    src.resume()  # 이미 풀려 있어도 오류 없이 끝난다


def test_pause_timeout_unpauses_and_does_not_hang(sites):
    src = sites[0]
    tx = psycopg.connect(src.pgbouncer_app_dsn)
    tx.execute("SELECT 1")  # 열린 트랜잭션이 PAUSE를 붙잡는다
    try:
        start = time.monotonic()
        with pytest.raises(PauseTimeout):
            admin(src).pause(timeout_s=1)
        assert time.monotonic() - start < 4
        assert not admin(src).state().paused
    finally:
        tx.rollback()
        tx.close()
    # 붙잡혀 있던 PAUSE가 나중에 다시 걸리지 않는다
    time.sleep(1)
    assert not admin(src).state().paused


def test_pgbouncer_retarget_reload_and_back(sites, kube):
    src, dst = sites
    pool, cfg = admin(dst), PgBouncerConfig(kube, dst.namespace, dst.pgbouncer_selector, dst.pgbouncer_ini)
    original = cfg.read()
    try:
        cfg.retarget(dst.pgbouncer_database, dst.db_host, dst.db_port, dst.db_name)
        pool.reload()
        assert pool.state().host == dst.db_host
    finally:
        cfg.retarget(dst.pgbouncer_database, src.db_host, src.db_port, src.db_name)
        pool.reload()
    assert pool.state().host == src.db_host
    assert cfg.read() == original


def test_fence_login_refusal_and_restore(sites):
    src = sites[0]
    db = DbControl(src.db_control_dsn)
    assert try_app_write(src.db_app_dsn, "test") == WriteRefusal.ACCEPTED
    try:
        db.set_login(src.app_role, False)
        assert db.terminate_sessions(src.app_role) >= 0
        assert try_app_write(src.db_app_dsn, "test") == WriteRefusal.REFUSED
    finally:
        db.set_login(src.app_role, True)
    assert try_app_write(src.db_app_dsn, "test") == WriteRefusal.ACCEPTED


def test_target_app_role_is_closed_before_cutover(sites):
    dst = sites[1]
    assert DbControl(dst.db_control_dsn).can_login(dst.app_role) is False
    assert try_app_write(dst.db_app_dsn, "test") == WriteRefusal.REFUSED


def test_unreachable_db_is_unknown_not_refused():
    dsn = "host=127.0.0.1 port=1 dbname=chat user=chat_app password=x"
    assert try_app_write(dsn, "test", timeout_s=1) == WriteRefusal.UNKNOWN


def test_terminate_sessions_ends_app_sessions(sites):
    src = sites[0]
    db = DbControl(src.db_control_dsn)
    held = psycopg.connect(src.db_app_dsn, autocommit=True)
    try:
        assert db.sessions(src.app_role) >= 1
        assert db.terminate_sessions(src.app_role) >= 1
        with pytest.raises(psycopg.OperationalError):
            held.execute("SELECT 1")
    finally:
        held.close()


def test_long_transaction_is_seen(sites):
    src = sites[0]
    tx = psycopg.connect(src.db_app_dsn)
    tx.execute("SELECT 1")
    try:
        time.sleep(0.3)
        assert DbControl(src.db_control_dsn).long_transactions(src.app_role, 200) >= 1
    finally:
        tx.rollback()
        tx.close()


def test_barrier_arrives_at_target(sites):
    src, dst = sites
    barrier = uuid.uuid4()
    DbControl(src.db_control_dsn).insert_barrier(barrier, epoch=1)
    deadline = time.monotonic() + 10
    while not DbControl(dst.db_control_dsn).has_barrier(barrier):
        assert time.monotonic() < deadline, "barrier did not arrive"
        time.sleep(0.2)


def test_subscription_disable_stops_worker_and_enable_restarts(sites):
    dst = sites[1]
    db = DbControl(dst.db_control_dsn)
    assert db.subscription("bravo_fwd").enabled
    try:
        db.set_subscription("bravo_fwd", False, dst.subscription_owner)
        sub = db.subscription("bravo_fwd")
        assert (sub.enabled, sub.workers) == (False, 0)
    finally:
        db.set_subscription("bravo_fwd", True, dst.subscription_owner)
    assert db.subscription("bravo_fwd").workers >= 1


def test_sequence_only_goes_up(sites):
    dst = sites[1]
    db = DbControl(dst.db_control_dsn)
    current = db.sequence_next("public.messages_id_seq")
    assert db.raise_sequence("public.messages_id_seq", current + 10) == current + 10
    assert db.sequence_next("public.messages_id_seq") == current + 10
    assert db.raise_sequence("public.messages_id_seq", 1) == current + 10
    assert db.sequence_next("public.messages_id_seq") == current + 10


def test_probe_write_through_pgbouncer(sites):
    for site in sites:
        assert app_write(site.pgbouncer_app_dsn, f"test-{site.namespace}") < 3


def test_pause_blocks_probe_until_resume(sites):
    src = sites[0]
    pool = admin(src)
    result = {}
    pool.pause(timeout_s=5)
    try:
        t = threading.Thread(target=lambda: result.setdefault("s", app_write(src.pgbouncer_app_dsn, "test", 10)))
        t.start()
        time.sleep(1)
        assert "s" not in result
    finally:
        pool.resume()
    t.join(5)
    assert result["s"] >= 1
