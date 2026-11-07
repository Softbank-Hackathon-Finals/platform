"""되돌리기(T20)를 모의 환경에서 돌린다 (spec 커밋 3: 완성 기준 1~5, ADR-0016).

시험마다 컷오버를 먼저 끝내 "온프렘이 주본, AWS는 역방향 복제로 따라옴" 상태를 만든 뒤, 출발지 · 목적지를
뒤바꾼 요청으로 되돌린다. 복제 경로 단절은 온프렘 publication에서 cutover.barrier를 잠시 빼서 만든다
(NetworkPolicy는 4회 중 2회 실제로 끊기지 않았다, ADR-0016). 빠진 동안 쓴 barrier 행은 복제되지 않으므로
publication을 되돌리기 전에 지운다(reset()이 양쪽 행 수가 같아지길 기다린다).
"""

import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from bravo_cutover.contract import CutoverRequest, Status
from bravo_cutover.remote.db import DbControl, WriteRefusal, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin
from bravo_cutover.runner import Cutover
from bravo_cutover.sites import registry_from_sites
from tests.mockenv_support import (
    DST_ENV,
    REFS,
    SRC_ENV,
    WriterWatch,
    mock_config,
    needs_mockenv,
    post_message,
    reset,
)
from tests.test_abort_mockenv import assert_source_is_writer_again
from tests.test_safe_hold_mockenv import Crash, crash, unreachable

pytestmark = needs_mockenv

ONPREM_APP, AWS_APP = "http://localhost:30580", "http://localhost:30480"


@pytest.fixture
def sides():
    config = mock_config()
    aws, onprem = config.sites[SRC_ENV], config.sites[DST_ENV]
    reset(aws, onprem)
    yield aws, onprem
    reset(aws, onprem)


def start(store, source: str, target: str, epoch: int) -> uuid.UUID:
    request = CutoverRequest(
        plan_id="plan-mock", epoch=epoch,
        source={"environment_id": source, "connection_ref": REFS[source]},
        target={"environment_id": target, "connection_ref": REFS[target]},
        write_pause_budget_ms=5000, deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    execution, _ = store.create(str(uuid.uuid4()), request, registry_from_sites(mock_config().sites).resolve(request))
    return execution.execution_id


def cut_over(store) -> int:
    """컷오버를 끝내고 그 epoch를 돌려준다. 되돌리기는 이보다 큰 epoch를 쓴다."""
    epoch = int(time.time() * 1000)
    execution_id = start(store, SRC_ENV, DST_ENV, epoch)
    Cutover(store, mock_config()).run(execution_id)
    assert store.get(execution_id).status == Status.DONE, store.get(execution_id).result
    return epoch


def start_failback(store, epoch: int) -> uuid.UUID:
    return start(store, DST_ENV, SRC_ENV, epoch + 1)


def admin_psql(site, sql: str) -> list[str]:
    """그 환경 DB에 postgres(슈퍼유저)로 붙는다. 시험 준비 · 정리 전용이다."""
    out = subprocess.run(
        ["kubectl", "--context", site.kube_context, "exec", "-i", "-n", site.namespace, "deploy/postgres", "--",
         "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "chat"],
        input=sql, text=True, capture_output=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


BARRIER_PUBLISHED = ("SELECT count(*) FROM pg_publication_tables "
                     "WHERE pubname = 'bravo_pub' AND schemaname = 'cutover' AND tablename = 'barrier';")


@pytest.fixture
def barrier_path(sides):
    """온프렘 → AWS 경로에서 barrier 표만 끊는다. 구독 worker는 살아 있어 사전 확인의 worker · 지연 검사는 통과한다.

    정리는 DROP 명령의 성공 여부가 아니라 publication의 실제 상태를 읽고 한다(DROP이 반영된 뒤 응답만 잃어도 복원한다).
    """
    aws, onprem = sides

    def cut(_run=None):
        admin_psql(onprem, "ALTER PUBLICATION bravo_pub DROP TABLE cutover.barrier;")

    yield cut
    if admin_psql(onprem, BARRIER_PUBLISHED) != ["1"]:
        arrived = set(admin_psql(aws, "SELECT barrier_id FROM cutover.barrier;"))
        orphans = [b for b in admin_psql(onprem, "SELECT barrier_id FROM cutover.barrier;") if b not in arrived]
        if orphans:
            admin_psql(onprem, "DELETE FROM cutover.barrier WHERE barrier_id IN ("
                       + ",".join(f"'{uuid.UUID(b)}'" for b in orphans) + ");")
        admin_psql(onprem, "ALTER PUBLICATION bravo_pub ADD TABLE cutover.barrier;")
    assert admin_psql(onprem, BARRIER_PUBLISHED) == ["1"], "barrier table was not restored to the publication"


def onprem_is_writer_again(aws, onprem) -> None:
    """되돌리기가 쓰기 전에 끝났다: 온프렘만 쓰기를 받고, 두 PgBouncer는 온프렘을 가리키며 풀려 있다."""
    assert try_app_write(onprem.db_app_dsn, "after-abort") == WriteRefusal.ACCEPTED
    assert try_app_write(aws.db_app_dsn, "after-abort") == WriteRefusal.REFUSED
    kube = Kube(aws.kube_context)
    for site in (aws, onprem):
        state = PgBouncerAdmin(site.pgbouncer_admin_dsn, site.pgbouncer_database).state()
        assert (state.host, state.paused) == (onprem.db_host, False)
        assert not any(WriteBlock(kube, site.namespace, site.app_selector).get().values())
    assert post_message(ONPREM_APP, uuid.uuid4(), "after aborted failback") == 201


# 기준 5 · 1: 정상 경로 ----------------------------------------------------------------------

def test_failback_brings_target_writes_back_to_origin(store, sides):
    aws, onprem = sides
    epoch = cut_over(store)
    keys = [uuid.uuid4() for _ in range(5)]
    for key in keys:
        assert post_message(ONPREM_APP, key, "written on onprem after cutover") == 201

    execution_id = start_failback(store, epoch)
    with WriterWatch(onprem, aws) as watch:
        Cutover(store, mock_config()).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result

    receipt = execution.result["receipt"]
    assert (receipt["direction"], receipt["source"], receipt["target"]) == ("failback", DST_ENV, SRC_ENV)
    assert receipt["verdict"] == "MATCH"
    assert execution.result["path_probe"]["arrived"] is True
    # 온프렘에 쓴 메시지가 모두 AWS에 있다
    found = DbControl(aws.db_control_dsn)._one(
        "SELECT count(*) FROM messages WHERE idempotency_key = ANY(%s)", ([str(k) for k in keys],))
    assert found == len(keys)
    # 되돌린 뒤: AWS만 쓰기, 정방향 복제가 다시 온프렘으로 따라간다
    assert_source_is_writer_again(aws, onprem)


# 기준 2: 쓰기를 막기 전에 경로 단절을 알아챈다 ---------------------------------------------------

def test_failback_refused_before_any_change_when_path_is_down(store, sides, barrier_path):
    aws, onprem = sides
    epoch = cut_over(store)
    barrier_path()
    execution_id = start_failback(store, epoch)
    with WriterWatch(onprem, aws) as watch:
        Cutover(store, mock_config(path_probe_timeout_s=2)).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["reason_code"] == "replication_path_unconfirmed"
    assert execution.result["remote_changes_started"] is False
    assert [s["step"] for s in execution.steps if s["status"] == "succeeded"] == ["abort"]
    onprem_is_writer_again(aws, onprem)


# 기준 3: 쓰기를 막은 뒤 따라잡지 못하면 AWS를 열지 않고 온프렘을 복원한다(D5) -----------------------------

def test_failback_restores_onprem_when_final_barrier_does_not_arrive(store, sides, barrier_path):
    aws, onprem = sides
    epoch = cut_over(store)
    execution_id = start_failback(store, epoch)
    with WriterWatch(onprem, aws) as watch:
        Cutover(store, mock_config(barrier_timeout_s=2), hooks={"before:compare": barrier_path}).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.ABORT_PRE_WRITE, execution.result
    assert execution.result["failed_step"] == "compare"
    assert "barrier did not arrive" in execution.result["error"]
    assert "source_login_restored" in execution.result["restored"]
    assert execution.committed_at is None
    onprem_is_writer_again(aws, onprem)


# 기준 4: 확정 뒤 확인할 수 없으면 아무것도 열지 않는다 -------------------------------------------------

def test_failback_target_unreachable_after_commit_holds(store, sides):
    aws, onprem = sides
    epoch = cut_over(store)
    execution_id = start_failback(store, epoch)
    with WriterWatch(onprem, aws) as watch:
        Cutover(store, mock_config(), hooks={"before:reconnect": unreachable("dst.db._dsn")}).run(execution_id)
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["reason"] == "failed after commit"
    assert execution.result["observed"]["unknown"] == ["target"]
    assert DbControl(aws.db_control_dsn).can_login(aws.app_role) is False  # AWS를 열지 않았다
    assert try_app_write(onprem.db_app_dsn, "hold") == WriteRefusal.REFUSED


def test_failback_restart_after_commit_holds(store, sides):
    aws, onprem = sides
    epoch = cut_over(store)
    execution_id = start_failback(store, epoch)
    with WriterWatch(onprem, aws) as watch:
        with pytest.raises(Crash):
            Cutover(store, mock_config(), hooks={"before:reconnect": crash}).run(execution_id)
        assert Cutover(store, mock_config()).recover() == [execution_id]
    watch.assert_safe()
    execution = store.get(execution_id)
    assert execution.status == Status.SAFE_HOLD, execution.result
    assert execution.result["observed"]["writable"] == []
    assert try_app_write(aws.db_app_dsn, "hold") == WriteRefusal.REFUSED
    assert try_app_write(onprem.db_app_dsn, "hold") == WriteRefusal.REFUSED
