"""SAFE_HOLD와 재시작 판정 (spec 커밋 7: 완성 기준 1 · 2 · 4, D6 · D10).

프로세스가 죽는 것은 BaseException(Crash)으로 흉내 낸다: 실행기의 실패 처리를 건너뛰어 실행이 RUNNING으로 남고,
원격 상태도 그 순간 그대로다. 그 뒤 새 실행기로 recover()를 부른다.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

from bravo_cutover.contract import Status
from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.db import DbControl, WriteRefusal, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin
from bravo_cutover.runner import Cutover
from bravo_cutover.store import Store
from tests.mockenv_support import WriterWatch, mock_config, needs_mockenv, reset
from tests.test_abort_mockenv import assert_source_is_writer_again
from tests.test_cutover_mockenv import start

pytestmark = needs_mockenv


class Crash(BaseException):
    """프로세스 종료 대역. except Exception에 잡히지 않는다."""


def crash(_run):
    raise Crash


@pytest.fixture
def sides():
    config = mock_config()
    src, dst = config.sites["env-aws"], config.sites["env-onprem"]
    reset(src, dst)
    yield src, dst
    reset(src, dst)


def crash_then_recover(store, src, dst, point):
    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        with pytest.raises(Crash):
            Cutover(store, mock_config(), hooks={point: crash}).run(execution_id)
        assert store.get(execution_id).status == Status.RUNNING
        assert Cutover(store, mock_config()).recover() == [execution_id]
    watch.assert_safe()
    return store.get(execution_id)


def pools(src, dst):
    return {s.environment_id: PgBouncerAdmin(s.pgbouncer_admin_dsn, s.pgbouncer_database).state() for s in (src, dst)}


def blocks(src, dst):
    kube = Kube(src.kube_context)
    return [v for s in (src, dst) for v in WriteBlock(kube, s.namespace, s.app_selector).get().values()]


@pytest.mark.parametrize("point", ["before:entry", "before:fence", "before:compare", "commit:subscriptions_changed"])
def test_restart_before_commit_aborts_and_restores_source(store, sides, point):
    src, dst = sides
    execution = crash_then_recover(store, src, dst, point)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert [s["step"] for s in execution.steps if s["step"] in ("recover", "abort")][:2] == ["recover", "abort"]
    assert_source_is_writer_again(src, dst)


def test_restart_after_commit_holds_without_opening(store, sides):
    """확정 뒤, 목적지를 열기 전에 죽었다 → SAFE_HOLD. 아무것도 열지 않는다(양쪽 쓰기 거부, PAUSE · 차단 유지)."""
    src, dst = sides
    execution = crash_then_recover(store, src, dst, "before:reconnect")
    assert execution.status == Status.SAFE_HOLD, execution.result
    observed = execution.result["observed"]
    assert observed["writable"] == [] and observed["unknown"] == []
    assert try_app_write(src.db_app_dsn, "hold") == WriteRefusal.REFUSED
    assert try_app_write(dst.db_app_dsn, "hold") == WriteRefusal.REFUSED
    assert all(state.paused for state in pools(src, dst).values())
    assert all(blocks(src, dst))


def test_restart_after_target_opened_holds_and_keeps_single_writer(store, sides):
    """목적지를 연 직후 죽었다 → SAFE_HOLD. 열린 목적지를 다시 닫지 않는다(D10). 쓰기 DB는 목적지 하나다."""
    src, dst = sides
    execution = crash_then_recover(store, src, dst, "reconnect:target_opened")
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["observed"]["writable"] == ["target"]
    assert try_app_write(src.db_app_dsn, "hold") == WriteRefusal.REFUSED
    assert DbControl(dst.db_control_dsn).can_login(dst.app_role) is True
    assert all(blocks(src, dst))  # 진입점 차단은 풀지 않았다


def unreachable(attr_path):
    def inject(run):
        obj = run
        *parents, last = attr_path.split(".")
        for name in parents:
            obj = getattr(obj, name)
        setattr(obj, last, "host=127.0.0.1 port=1 dbname=x user=x password=x connect_timeout=1")
    return inject


def test_target_unreachable_before_reconnect(store, sides):
    """기준 2: 확정 뒤 목적지 DB에 닿지 않는다 → 새로 여는 조작 없이 SAFE_HOLD, 목적지 상태는 unknown."""
    src, dst = sides
    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        Cutover(store, mock_config(), hooks={"before:reconnect": unreachable("dst.db._dsn")}).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD and execution.result["reason"] == "failed after commit"
    assert execution.result["observed"]["unknown"] == ["target"]
    assert DbControl(dst.db_control_dsn).can_login(dst.app_role) is False  # 실제로도 열지 않았다
    assert all(state.paused for state in pools(src, dst).values())
    assert all(blocks(src, dst))


def test_target_pgbouncer_unreachable_after_target_opened(store, sides):
    """기준 2: 목적지를 연 뒤 목적지 PgBouncer에 닿지 않는다 → SAFE_HOLD, 진입점 차단은 그대로 둔다."""
    src, dst = sides
    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        Cutover(store, mock_config(), hooks={"reconnect:target_opened": unreachable("dst.pool._dsn")}).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    observed = execution.result["observed"]
    assert observed["writable"] == ["target"] and observed["sides"]["target"]["pgbouncer"] == "unknown"
    assert all(blocks(src, dst))


def post_status(port: int) -> int:
    req = urllib.request.Request(
        f"http://localhost:{port}/api/messages", method="POST",
        data=json.dumps({"idempotency_key": str(uuid.uuid4()), "author": "t", "body": "during hold"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    for _ in range(10):  # 재시작 직후 종료 중인 파드에 닿으면 연결이 끊긴다
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status
        except urllib.error.HTTPError as e:
            return e.code
        except TimeoutError:
            return 0  # 응답 없음: 쓰기가 받아들여지지 않았다
        except (ConnectionError, urllib.error.URLError):
            time.sleep(1)
    raise AssertionError("app did not answer")


def test_pod_restarts_during_hold_do_not_open_writes(store, sides):
    """기준 1: SAFE_HOLD 중 PgBouncer · 앱 파드가 재시작해 PAUSE · 차단이 풀려도 출발지 NOLOGIN이 쓰기를 막는다."""
    src, dst = sides
    execution = crash_then_recover(store, src, dst, "before:reconnect")
    assert execution.status == Status.SAFE_HOLD
    ctx = ["kubectl", "--context", src.kube_context, "-n", src.namespace]
    with WriterWatch(src, dst) as watch:
        subprocess.run([*ctx, "rollout", "restart", "deploy/pgbouncer", "deploy/demo-chat"], check=True, capture_output=True)
        for deploy in ("pgbouncer", "demo-chat"):
            subprocess.run([*ctx, "rollout", "status", f"deploy/{deploy}", "--timeout=120s"], check=True, capture_output=True)
        assert not PgBouncerAdmin(src.pgbouncer_admin_dsn, src.pgbouncer_database).state().paused
        counts = [DbControl(s.db_control_dsn).row_count("public.messages") for s in (src, dst)]
        # 새 파드는 차단이 풀린 채 떴지만 DB가 거부한다(503 또는 응답 없음)
        assert post_status(30480) != 201
        assert [DbControl(s.db_control_dsn).row_count("public.messages") for s in (src, dst)] == counts
    watch.assert_safe()
    assert try_app_write(src.db_app_dsn, "hold") == WriteRefusal.REFUSED
    assert try_app_write(dst.db_app_dsn, "hold") == WriteRefusal.REFUSED


def test_pending_execution_at_restart_is_aborted_without_remote_effect(store, sides):
    execution_id = start(store)
    assert Cutover(store, mock_config()).recover() == [execution_id]
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE and execution.steps == []


def test_only_one_executor_process(postgres_dsn, store):
    first, second = Store(postgres_dsn), Store(postgres_dsn)
    try:
        assert first.acquire_singleton() is True
        assert second.acquire_singleton() is False
        first.close()
        assert second.acquire_singleton() is True
    finally:
        second.close()


def test_restart_after_deadline_still_decides(store, sides):
    """deadline이 지난 뒤 재시작해도 저장된 요청을 읽어 판정한다(검토 갭 2)."""
    src, dst = sides
    execution_id = start(store, deadline_s=4)
    with pytest.raises(Crash):
        Cutover(store, mock_config(), hooks={"before:fence": crash}).run(execution_id)
    time.sleep(4.5)
    assert Cutover(store, mock_config()).recover() == [execution_id]
    assert store.get(execution_id).status == Status.ABORT_PRE_WRITE
    assert_source_is_writer_again(src, dst)


@pytest.mark.parametrize(("point", "decided"), [("before:fence", Status.ABORT_PRE_WRITE),
                                                ("before:reconnect", Status.SAFE_HOLD)])
def test_old_executor_stops_after_takeover(store, postgres_dsn, sides, point, decided):
    """잠금을 잃은 앞 Executor가 아직 살아 있을 때 새 Executor가 넘겨받으면, 앞 Executor는 다음 조작 전에 멈춘다.
    넘겨받은 쪽의 판정만 남고 목적지는 열리지 않는다(검토 갭 1)."""
    src, dst = sides
    newer = Store(postgres_dsn)

    def takeover(_run):
        Cutover(newer, mock_config()).recover()

    try:
        execution_id = start(store)
        with WriterWatch(src, dst) as watch:
            Cutover(store, mock_config(), hooks={point: takeover}).run(execution_id)
        watch.assert_safe()
        execution = store.get(execution_id)
        assert execution.status == decided, execution.result
        assert DbControl(dst.db_control_dsn).can_login(dst.app_role) is False
        # 넘겨받은 지점의 단계는 앞 Executor가 시작하지 않았다
        assert not [e for e in execution.steps if e["step"] == point.split(":")[1] and e["status"] == "started"]
    finally:
        newer.close()


def test_configmap_conflict_holds_and_pod_regeneration_stays_single_writer(store, sides):
    """#116 리뷰: ConfigMap 영구 반영이 충돌하면 SAFE_HOLD(lease 유지). 그 상태에서 PgBouncer가 재생성돼
    ConfigMap의 옛 설정으로 떠도 쓰기 DB는 하나다(출발지는 NOLOGIN)."""
    src, dst = sides
    ctx = ["kubectl", "--context", dst.kube_context, "-n", dst.namespace]

    def concurrent_edit(_run):  # 읽은 뒤 다른 쪽이 먼저 ConfigMap을 바꿨다
        subprocess.run([*ctx, "annotate", "configmap", dst.pgbouncer_configmap, f"test/edit={uuid.uuid4().hex}",
                        "--overwrite"], check=True, capture_output=True)

    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        Cutover(store, mock_config(), hooks={"persist:read:env-onprem": concurrent_edit}).run(execution_id)
        execution = store.get(execution_id)
        assert execution.status == Status.SAFE_HOLD and execution.result["failed_step"] == "persist", execution.result
        subprocess.run([*ctx, "rollout", "restart", "deploy/pgbouncer"], check=True, capture_output=True)
        subprocess.run([*ctx, "rollout", "status", "deploy/pgbouncer", "--timeout=120s"], check=True, capture_output=True)
        # 재생성 직후에는 NodePort가 종료 중인 옛 파드로 붙어 끊길 수 있어 다시 읽는다
        deadline = time.monotonic() + 30
        while True:
            try:
                state = PgBouncerAdmin(dst.pgbouncer_admin_dsn, dst.pgbouncer_database).state()
                break
            except Unconfirmed:
                assert time.monotonic() < deadline
                time.sleep(0.5)
        assert state.host == src.db_host  # 재생성된 파드는 ConfigMap의 옛 설정(출발지)으로 떴다
        assert try_app_write(dst.pgbouncer_app_dsn, "regenerated") == WriteRefusal.REFUSED
    watch.assert_safe()
    from bravo_cutover.store import LeaseHeld

    with pytest.raises(LeaseHeld):
        start(store)  # SAFE_HOLD가 lease를 쥐고 있다
