"""콘솔용 조회 API. 지표 저장소는 가짜 전송으로 대신하고, 실행한 조회식을 기록해 고정 조회식만 쓰는지 본다."""

import httpx
import pytest
from fastapi.testclient import TestClient

from bravo_observability.api import Prometheus, bearer_token, create_app, deny_all

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeStore:
    def __init__(self, results=None, alerts=None, down=False):
        self.results = results or {}
        self.alert_rows = alerts or []
        self.down = down
        self.seen: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/api/v1/alerts":
            return httpx.Response(200, json={"status": "success", "data": {"alerts": self.alert_rows}})
        q = request.url.params["query"]
        self.seen.append(q)
        result = next((v for k, v in self.results.items() if k in q), [])
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})


def client(store, envs=("aws", "onprem"), authorize=None):
    prom = Prometheus(httpx.Client(base_url="https://prom", transport=httpx.MockTransport(store.handler)))
    return TestClient(create_app(prom, list(envs), authorize=authorize or bearer_token(TOKEN)))


def vec(env, value):
    return {"metric": {"bravo_env": env}, "value": [0, str(value)]}


def test_requires_token_and_default_denies():
    store = FakeStore()
    assert client(store).get("/v1/observability/environments").status_code == 401
    assert client(store).get("/v1/observability/environments", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client(store, authorize=deny_all).get("/v1/observability/environments", headers=AUTH).status_code == 401
    assert client(store).get("/healthz").json() == {"status": "ok"}
    assert store.seen == []


def test_environment_states_ok_stale_and_absent():
    store = FakeStore(results={"timestamp(up": [vec("aws", 4.2), vec("onprem", 31.0), vec("intruder", 1)]})
    body = client(store, envs=("aws", "onprem", "gcp")).get("/v1/observability/environments", headers=AUTH).json()
    assert body["stale_after_seconds"] == 30
    assert body["environments"] == [
        {"environment": "aws", "state": "OK", "last_received_seconds_ago": 4.2},
        {"environment": "onprem", "state": "UNOBSERVABLE", "last_received_seconds_ago": 31.0},
        {"environment": "gcp", "state": "UNOBSERVABLE", "last_received_seconds_ago": None},
    ]
    # 생존 신호는 신뢰 라벨로만 고른다
    assert store.seen == ['time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))']


def test_store_unavailable_is_503_not_zero():
    r = client(FakeStore(down=True)).get("/v1/observability/environments", headers=AUTH)
    assert r.status_code == 503 and r.json()["detail"] == "store_unavailable"


def test_unregistered_environment_never_reaches_query():
    store = FakeStore()
    c = client(store)
    for env in ("gcp", 'onprem"} or vector(1) #', "ONPREM"):
        assert c.get(f"/v1/observability/environments/{env}/http", headers=AUTH).status_code == 404
    assert store.seen == []


def test_http_view_uses_fixed_queries_and_null_without_traffic():
    store = FakeStore(results={"sum(rate(http_server_request_duration_seconds_count{bravo_env=\"onprem\"}[1m]))": []})
    body = client(store).get("/v1/observability/environments/onprem/http", headers=AUTH).json()
    assert (body["requests_per_second"], body["error_ratio"], body["p95_seconds"]) == (None, None, None)
    assert len(store.seen) == 3 and all('bravo_env="onprem"' in q for q in store.seen)


def test_http_view_values():
    store = FakeStore(
        results={
            "histogram_quantile": [vec("onprem", 0.25)],
            '=~"5.."': [vec("onprem", 0.02)],
            "sum(rate(http_server_request_duration_seconds_count": [vec("onprem", 20)],
        }
    )
    body = client(store).get("/v1/observability/environments/onprem/http", headers=AUTH).json()
    assert (body["requests_per_second"], body["error_ratio"], body["p95_seconds"]) == (20.0, 0.02, 0.25)


def test_alerts_only_bravo_rules():
    store = FakeStore(
        alerts=[
            {"labels": {"alertname": "BravoEnvUnobservable", "bravo_env": "onprem", "severity": "page"}, "annotations": {"summary": "s", "runbook": "r"}, "state": "firing", "activeAt": "2026-11-04T00:00:00Z"},
            {"labels": {"alertname": "Watchdog"}, "state": "firing"},
        ]
    )
    body = client(store).get("/v1/observability/alerts", headers=AUTH).json()
    assert body["alerts"] == [
        {"alertname": "BravoEnvUnobservable", "state": "firing", "environment": "onprem", "severity": "page", "summary": "s", "runbook": "r", "active_at": "2026-11-04T00:00:00Z"}
    ]


@pytest.mark.parametrize("envs", [[], ["aws", "aws"], ["Aws"], ['x"}']])
def test_rejects_bad_environment_lists(envs):
    with pytest.raises(ValueError):
        create_app(Prometheus(httpx.Client()), envs)
