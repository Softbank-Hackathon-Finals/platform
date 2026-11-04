"""8단계 컷오버를 모의 환경에서 끝까지 돌린다 (spec 커밋 5: 완성 기준 1 · 7 · 7a).

모의 환경(testing/mockenv.py up)과 실행 기록용 Postgres(docker)가 필요하다. 시험마다 컷오버 전 상태로 되돌린다.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from bravo_cutover.contract import CutoverRequest, Status
from bravo_cutover.db_fingerprint import HASH_RULE
from bravo_cutover.remote.db import DbControl, WriteRefusal, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin
from bravo_cutover.runner import STEPS, Cutover
from bravo_cutover.sites import registry_from_sites
from tests.mockenv_support import WriterWatch, mock_config, needs_mockenv, post_message, reset

pytestmark = needs_mockenv


@pytest.fixture
def config():
    return mock_config()


@pytest.fixture
def sides(config):
    src, dst = config.sites["env-aws"], config.sites["env-onprem"]
    reset(src, dst)
    yield src, dst
    reset(src, dst)


def start(store, epoch=None, deadline_s=120):
    request = CutoverRequest(
        plan_id="plan-mock", epoch=epoch or int(time.time() * 1000),
        source={"environment_id": "env-aws", "connection_ref": "mock/src"},
        target={"environment_id": "env-onprem", "connection_ref": "mock/dst"},
        write_pause_budget_ms=5000, deadline=datetime.now(UTC) + timedelta(seconds=deadline_s),
    )
    execution, _ = store.create(str(uuid.uuid4()), request, registry_from_sites(mock_config().sites).resolve(request))
    return execution.execution_id


def post(port: int) -> int:
    return post_message(f"http://localhost:{port}", uuid.uuid4(), "after cutover")


def test_cutover_happy_path(store, config, sides):
    src, dst = sides
    execution_id = start(store)
    with WriterWatch(src, dst) as watch:
        Cutover(store, config).run(execution_id)
    execution = store.get(execution_id)
    assert execution.status == Status.DONE, execution.result

    # 기준 1: 어느 순간에도 앱 역할이 로그인할 수 있는 DB는 최대 하나
    assert watch.samples[0][1:] == (True, False) and watch.samples[-1][1:] == (False, True)
    watch.assert_safe()

    # 기준 7: 결과와 영수증
    result = execution.result
    print("write_pause_ms", result["write_pause_ms"], "stages_ms", result["stages_ms"], "acks", result["path_acks_ms"])
    assert set(result["stages_ms"]) == set(STEPS)
    assert set(result["path_acks_ms"]) == {"entry:env-aws", "entry:env-onprem", "pool:env-aws", "pool:env-onprem"}
    assert result["write_pause_ms"] == max(result["path_acks_ms"].values())
    receipt = result["receipt"]
    assert (receipt["verdict"], receipt["hash_rule"]) == ("MATCH", HASH_RULE)  # T13 DB 지문 비교
    assert all(t["rows_src"] == t["rows_dst"] for t in receipt["tables"])
    assert receipt["sequences"][0]["dst_set"] >= receipt["sequences"][0]["src_next"]
    assert execution.committed_at is not None
    assert [s["step"] for s in execution.steps if s["status"] == "succeeded"] == list(STEPS)

    # 이후 상태: 목적지만 쓰기, 역방향만 켜짐, 두 PgBouncer는 목적지 · RESUME, 진입점 차단 해제
    sdb, ddb = DbControl(src.db_control_dsn), DbControl(dst.db_control_dsn)
    assert try_app_write(src.db_app_dsn, "after") == WriteRefusal.REFUSED
    assert try_app_write(dst.db_app_dsn, "after") == WriteRefusal.ACCEPTED
    assert sdb.subscription(src.subscription).enabled and not ddb.subscription(dst.subscription).enabled
    kube = Kube(src.kube_context)
    for site in (src, dst):
        state = PgBouncerAdmin(site.pgbouncer_admin_dsn, site.pgbouncer_database).state()
        assert (state.host, state.paused) == (dst.db_host, False)
        assert not any(WriteBlock(kube, site.namespace, site.app_selector).get().values())
        assert dst.db_host in kube.read_configmap(site.namespace, site.pgbouncer_configmap, site.pgbouncer_configmap_key)
    # 두 환경의 앱 모두 새 메시지를 쓸 수 있다(목적지 DB로 간다)
    before = ddb.row_count("public.messages")
    assert post(30480) == 201 and post(30580) == 201
    assert ddb.row_count("public.messages") == before + 2


def test_write_pause_ends_at_the_last_path_ack(store, config, sides):
    """한 경로의 차단 해제를 2초 늦추면 쓰기 정지 시간이 그만큼 늘어난다(첫 ACK에서 끝내지 않는다)."""
    execution_id = start(store)
    Cutover(store, config, hooks={"release:env-onprem": lambda _run: time.sleep(2)}).run(execution_id)
    result = store.get(execution_id).result
    assert store.get(execution_id).status == Status.DONE
    acks = result["path_acks_ms"]
    # 경로는 동시에 열린다. 늦춘 경로만 2초 뒤에 ACK하고(시계 오차 여유 200ms), 그 ACK가 쓰기 정지의 끝이다.
    assert acks["entry:env-onprem"] - max(v for k, v in acks.items() if k != "entry:env-onprem") >= 1800
    assert result["write_pause_ms"] == acks["entry:env-onprem"] >= 2000


def test_api_runs_cutover_in_background_and_reports_state(store, config, sides):
    """T10 호출 경로: POST는 202로 바로 돌아오고, 상태 조회로 진행과 결과를 읽는다(spec D1)."""
    from bravo_cutover.runner import ThreadRunner
    from tests.test_api import command_client

    client = command_client(store, ThreadRunner(Cutover(store, config)), registry_from_sites(config.sites))
    request = {
        "plan_id": "plan-api", "epoch": int(time.time() * 1000),
        "source": {"environment_id": "env-aws", "connection_ref": "mock/src"},
        "target": {"environment_id": "env-onprem", "connection_ref": "mock/dst"},
        "write_pause_budget_ms": 5000, "deadline": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
    }
    accepted = client.post("/v1/cutovers", json=request, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert accepted.status_code == 202
    deadline = time.monotonic() + 30
    while (state := client.get(f"/v1/cutovers/{accepted.json()['execution_id']}").json())["status"] in ("PENDING", "RUNNING"):
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert state["status"] == "DONE" and state["committed"] is True
    assert {s["status"] for s in state["steps"]} <= {"started", "succeeded"}
