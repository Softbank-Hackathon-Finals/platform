"""실행 요청 API: 멱등 키 · epoch · lease · 상태 조회 (spec 완성 기준 5 · 6 · 8)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bravo_cutover.api import create_app
from bravo_cutover.contract import Status
from bravo_cutover.resources import Registry, Resource

# 서버에 등록한 실제 DB 자원. env-alias는 env-aws와 같은 DB를 다른 이름으로 등록한 것이다.
REGISTRY = Registry([
    Resource(environment_id="env-aws", resource_id="cnpg-aws/chat", connection_ref="secret/source"),
    Resource(environment_id="env-onprem", resource_id="cnpg-onprem/chat", connection_ref="secret/target"),
    Resource(environment_id="env-alias", resource_id="cnpg-aws/chat", connection_ref="secret/source"),
    Resource(environment_id="env-gcp", resource_id="cnpg-gcp/chat", connection_ref="secret/gcp"),
    Resource(environment_id="env-gcp2", resource_id="cnpg-gcp2/chat", connection_ref="secret/gcp2"),
])
ENDPOINTS = {
    "aws": {"environment_id": "env-aws", "connection_ref": "secret/source"},
    "onprem": {"environment_id": "env-onprem", "connection_ref": "secret/target"},
    "alias": {"environment_id": "env-alias", "connection_ref": "secret/source"},
    "gcp": {"environment_id": "env-gcp", "connection_ref": "secret/gcp"},
    "gcp2": {"environment_id": "env-gcp2", "connection_ref": "secret/gcp2"},
}


class CountingRunner:
    """원격 조작의 대역. 불린 횟수로 "원격 효과 없이 거부"를 확인한다."""

    def __init__(self):
        self.submitted = []

    def submit(self, execution_id):
        self.submitted.append(execution_id)


def body(epoch=1, plan_id="plan-demo", **override):
    request = {
        "plan_id": plan_id,
        "epoch": epoch,
        "source": {"environment_id": "env-aws", "connection_ref": "secret/source"},
        "target": {"environment_id": "env-onprem", "connection_ref": "secret/target"},
        "write_pause_budget_ms": 5000,
        "deadline": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    return request | override


@pytest.fixture
def runner():
    return CountingRunner()


@pytest.fixture
def client(store, runner):
    return TestClient(create_app(store, runner, REGISTRY))


def post(client, key, request):
    return client.post("/v1/cutovers", json=request, headers={"Idempotency-Key": key})


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_new_request_is_recorded_and_submitted_once(client, runner):
    response = post(client, "k1", body())
    assert response.status_code == 202
    execution = response.json()
    assert execution["status"] == Status.PENDING
    assert [str(x) for x in runner.submitted] == [execution["execution_id"]]


def test_same_key_same_request_returns_stored_execution_without_rerun(client, runner):
    request = body()
    first = post(client, "k1", request).json()
    again = post(client, "k1", request)
    assert again.status_code == 200
    assert again.json()["execution_id"] == first["execution_id"]
    assert len(runner.submitted) == 1


def test_same_key_different_request_is_rejected(client, runner):
    post(client, "k1", body())
    response = post(client, "k1", body(write_pause_budget_ms=9000))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_key_conflict"
    assert len(runner.submitted) == 1


def test_concurrent_same_key_runs_once(client, runner):
    request = body()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(client, "k1", request), range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["execution_id"] for r in responses}) == 1
    assert len(runner.submitted) == 1


@pytest.mark.parametrize("epoch", [1, 2])
def test_epoch_not_above_highest_seen_is_rejected(client, store, runner, epoch):
    first = post(client, "k1", body(epoch=2)).json()
    store.set_status(first["execution_id"], Status.DONE)
    response = post(client, "k2", body(epoch=epoch))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_epoch"
    assert len(runner.submitted) == 1


@pytest.mark.parametrize("holding", [Status.PENDING, Status.RUNNING, Status.SAFE_HOLD])
def test_lease_blocks_second_execution_on_same_db_pair(client, store, runner, holding):
    first = post(client, "k1", body(epoch=1)).json()
    store.set_status(first["execution_id"], holding)
    response = post(client, "k2", body(epoch=2, plan_id="plan-other"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "lease_held"
    assert len(runner.submitted) == 1


@pytest.mark.parametrize("finished", [Status.DONE, Status.ABORT_PRE_WRITE])
def test_finished_execution_releases_lease(client, store, finished):
    first = post(client, "k1", body(epoch=1)).json()
    store.set_status(first["execution_id"], finished)
    assert post(client, "k2", body(epoch=2)).status_code == 202


def test_failback_direction_shares_epoch_and_lease(client, runner):
    """되돌리기(출발지 · 목적지가 뒤바뀜)도 같은 DB 쌍이다."""
    post(client, "k1", body(epoch=1))
    reverse = body(epoch=2, source=body()["target"], target=body()["source"])
    assert post(client, "k2", reverse).json()["detail"]["code"] == "lease_held"


def test_lost_response_can_be_recovered_by_reading_state(client):
    created = post(client, "k1", body(epoch=3)).json()
    by_id = client.get(f"/v1/cutovers/{created['execution_id']}")
    assert by_id.status_code == 200 and by_id.json()["status"] == Status.PENDING
    by_plan = client.get("/v1/cutovers", params={"plan_id": "plan-demo", "epoch": 3}).json()
    assert [e["execution_id"] for e in by_plan] == [created["execution_id"]]


def test_state_survives_new_process(store, postgres_dsn):
    from bravo_cutover.store import Store

    created = post(TestClient(create_app(store, CountingRunner(), REGISTRY)), "k1", body()).json()
    other = Store(postgres_dsn)
    try:
        assert other.get(created["execution_id"]).status == Status.PENDING
    finally:
        other.close()


def test_same_key_after_deadline_returns_stored_execution(client, runner):
    """deadline이 지난 뒤 같은 키로 재요청해도(응답 유실 후 재시도) 저장된 실행을 돌려준다."""
    import time

    request = body(deadline=(datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    first = post(client, "k1", request).json()
    time.sleep(1.2)
    again = post(client, "k1", request)
    assert again.status_code == 200 and again.json()["execution_id"] == first["execution_id"]
    assert len(runner.submitted) == 1
    late = post(client, "k2", request | {"epoch": 2})
    assert late.status_code == 422 and late.json()["detail"]["code"] == "deadline_passed"


def test_stored_request_with_past_deadline_still_parses(store):
    """재시작 판정은 저장된 요청을 다시 읽는다. 지난 deadline 때문에 읽기가 실패하면 안 된다."""
    from bravo_cutover.contract import CutoverRequest

    stored = body(deadline=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    assert CutoverRequest.model_validate(stored).deadline_passed()


def test_unknown_execution_is_404(client):
    assert client.get("/v1/cutovers/00000000-0000-0000-0000-000000000000").status_code == 404


@pytest.mark.parametrize(
    ("headers", "request_body", "status"),
    [
        ({}, body(), 400),
        ({"Idempotency-Key": "k"}, body(deadline=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()), 422),
        ({"Idempotency-Key": "k"}, body(deadline="2030-01-01T00:00:00"), 422),  # 시간대 없음
        ({"Idempotency-Key": "k"}, body(epoch=0), 422),
        ({"Idempotency-Key": "k"}, body(password="x"), 422),  # 계약 밖 필드
    ],
)
def test_invalid_requests_are_rejected_without_remote_effect(client, runner, headers, request_body, status):
    assert client.post("/v1/cutovers", json=request_body, headers=headers).status_code == status
    assert runner.submitted == []


def pair(src, dst, epoch, plan_id="plan-demo"):
    return body(epoch=epoch, plan_id=plan_id, source=ENDPOINTS[src], target=ENDPOINTS[dst])


@pytest.mark.parametrize(("second", "why"), [
    (("aws", "onprem"), "같은 쌍"),
    (("onprem", "aws"), "역방향"),
    (("alias", "onprem"), "별칭(같은 DB를 다른 이름으로 등록)"),
    (("aws", "gcp"), "출발지 DB 공유"),
    (("gcp", "onprem"), "목적지 DB 공유"),
])
def test_overlapping_real_resources_are_not_run_twice(client, runner, second, why):
    """#115 리뷰: 실제 DB 자원이 하나라도 겹치면 두 번째 요청은 실행기에 가지 않는다."""
    assert post(client, "k1", pair("aws", "onprem", epoch=1)).status_code == 202
    response = post(client, "k2", pair(*second, epoch=2, plan_id="plan-other"))
    assert response.status_code == 409, why
    assert response.json()["detail"]["code"] == "lease_held"
    assert len(runner.submitted) == 1


def test_safe_hold_keeps_every_resource_locked(client, store, runner):
    first = post(client, "k1", pair("aws", "onprem", epoch=1)).json()
    store.set_status(first["execution_id"], Status.SAFE_HOLD)
    for key, (src, dst) in enumerate([("aws", "gcp"), ("gcp", "onprem"), ("alias", "gcp2")]):
        assert post(client, f"k{key + 2}", pair(src, dst, epoch=key + 2)).json()["detail"]["code"] == "lease_held"
    assert len(runner.submitted) == 1


def test_disjoint_resource_pairs_run_independently(client, runner):
    assert post(client, "k1", pair("aws", "onprem", epoch=1)).status_code == 202
    assert post(client, "k2", pair("gcp", "gcp2", epoch=1)).status_code == 202
    assert len(runner.submitted) == 2


def test_epoch_is_tracked_per_resource(client, store):
    first = post(client, "k1", pair("aws", "onprem", epoch=5)).json()
    store.set_status(first["execution_id"], Status.DONE)
    assert post(client, "k2", pair("gcp", "gcp2", epoch=1)).status_code == 202  # 겹치지 않는 자원
    late = post(client, "k3", pair("alias", "gcp", epoch=3))  # aws 자원은 이미 5까지 받았다
    assert late.json()["detail"]["code"] in ("stale_epoch", "lease_held")
    assert late.status_code == 409


def test_concurrent_overlapping_requests_accept_one(client, runner):
    requests = [("k-a", pair("aws", "onprem", epoch=1)), ("k-b", pair("alias", "gcp", epoch=2)),
                ("k-c", pair("gcp", "onprem", epoch=3)), ("k-d", pair("onprem", "aws", epoch=4))]
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(lambda kr: post(client, kr[0], kr[1]).status_code, requests))
    assert codes.count(202) == 1 and len(runner.submitted) == 1


@pytest.mark.parametrize(("source", "target", "code"), [
    ({"environment_id": "env-unknown", "connection_ref": "secret/source"}, ENDPOINTS["onprem"], "unknown_environment"),
    ({"environment_id": "env-aws", "connection_ref": "secret/other"}, ENDPOINTS["onprem"], "connection_ref_mismatch"),
    (ENDPOINTS["aws"], ENDPOINTS["alias"], "same_resource"),
])
def test_requests_not_bound_to_registered_resources_are_rejected(client, runner, source, target, code):
    response = post(client, "k1", body(source=source, target=target))
    assert response.status_code == 422 and response.json()["detail"]["code"] == code
    assert runner.submitted == []
