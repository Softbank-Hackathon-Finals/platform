"""콘솔용 조회 API. 지표 저장소는 가짜 전송으로 대신하고, 실행한 조회식을 기록해 고정 조회식만 쓰는지 본다."""

import json
from pathlib import Path

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
    assert store.seen == ['time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}) and (up{bravo_signal="collector-self"} == 1))']


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
    store = FakeStore(results={"timestamp(up": [vec("onprem", 2)]})
    body = client(store).get("/v1/observability/environments/onprem/http", headers=AUTH).json()
    assert (body["requests_per_second"], body["error_ratio"], body["p95_seconds"]) == (None, None, None)
    assert len(store.seen) == 4 and all('bravo_env="onprem"' in q for q in store.seen)


def test_http_view_values():
    store = FakeStore(
        results={
            "timestamp(up": [vec("onprem", 2)],
            "histogram_quantile": [vec("onprem", 0.25)],
            '=~"5.."': [vec("onprem", 0.02)],
            "sum(rate(http_server_request_duration_seconds_count": [vec("onprem", 20)],
        }
    )
    body = client(store).get("/v1/observability/environments/onprem/http", headers=AUTH).json()
    assert (body["requests_per_second"], body["error_ratio"], body["p95_seconds"]) == (20.0, 0.02, 0.25)


def test_live_query_matches_promtool_zero_error_and_absent_series_cases():
    store = FakeStore(results={"timestamp(up": [vec("aws", 2)]})
    client(store).get("/v1/observability/environments/aws/http", headers=AUTH)
    fixture = json.loads((Path(__file__).parents[1] / "testing/http_queries_test.yml").read_text())
    actual = next(query for query in store.seen if '=~"5.."' in query)
    assert all(test["promql_expr_test"][0]["expr"] in {actual, f"({actual}) >= 0"} for test in fixture["tests"])


@pytest.mark.parametrize("rate,expected", [(20, 0.0), (0, None)])
def test_zero_error_ratio_requires_positive_observed_traffic(rate, expected):
    store = FakeStore(results={
        "timestamp(up": [vec("aws", 2)],
        '=~"5.."': [vec("aws", 0)],
        "sum(rate": [vec("aws", rate)],
    })
    body = client(store).get("/v1/observability/environments/aws/http", headers=AUTH).json()
    assert body["error_ratio"] == expected


def test_alerts_only_bravo_rules():
    store = FakeStore(
        alerts=[
            {"labels": {"alertname": "BravoEnvUnobservable", "bravo_env": "onprem", "severity": "page"}, "annotations": {"summary": "s", "runbook": "r"}, "state": "firing", "activeAt": "2026-11-04T00:00:00Z"},
            {"labels": {"alertname": "Watchdog"}, "state": "firing", "activeAt": "2026-11-04T00:00:00Z"},
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


def test_database_view_states_and_slots():
    def m(value, **labels):
        return {"metric": labels, "value": [0, str(value)]}

    store = FakeStore(
        results={
            "timestamp(up": [vec("aws", 2)],
            "cnpg_collector_up": [
                m(1, cnpg_cluster="source-db", pod="source-db-1", instance_role="primary"),
                m(0, cnpg_cluster="source-db", pod="source-db-2", instance_role="replica"),
            ],
            "pg_wal_lsn_diff": [m(4096, cnpg_cluster="source-db", slot_name="sub_target", slot_type="logical")],
            "replication_slots_active": [m(1, cnpg_cluster="source-db", slot_name="sub_target")],
        }
    )
    body = client(store).get("/v1/observability/environments/aws/database", headers=AUTH).json()
    assert body["state"] == "DOWN"
    assert [i["up"] for i in body["instances"]] == [True, False]
    assert body["replication_slots"] == [{"cluster": "source-db", "slot": "sub_target", "type": "logical", "lag_bytes": 4096.0, "active": True}]
    assert all('bravo_env="aws"' in q for q in store.seen)


def test_database_view_without_metrics_is_unobservable():
    body = client(FakeStore()).get("/v1/observability/environments/aws/database", headers=AUTH).json()
    assert (body["state"], body["instances"], body["replication_slots"]) == ("UNOBSERVABLE", [], [])


@pytest.mark.parametrize("age", [31, -0.01, "NaN", "+Inf", "-Inf"])
def test_unhealthy_heartbeat_clears_old_metrics_without_querying_them(age):
    store = FakeStore(results={"timestamp(up": [vec("aws", age)], "cnpg_collector_up": [vec("aws", 1)], "sum(rate": [vec("aws", 12)]})
    c = client(store)
    http = c.get("/v1/observability/environments/aws/http", headers=AUTH).json()
    db = c.get("/v1/observability/environments/aws/database", headers=AUTH).json()
    assert http["state"] == db["state"] == "UNOBSERVABLE"
    assert http["requests_per_second"] is http["error_ratio"] is http["p95_seconds"] is None
    assert db["instances"] == db["replication_slots"] == []
    assert len(store.seen) == 2
    assert all("timestamp(up" in q and "== 1" in q for q in store.seen)


@pytest.mark.parametrize("age", [-1, "NaN", "Inf"])
def test_invalid_environment_age_is_never_ok(age):
    body = client(FakeStore(results={"timestamp(up": [vec("aws", age)]})).get("/v1/observability/environments", headers=AUTH).json()
    assert body["environments"][0]["state"] == "UNOBSERVABLE"
    assert body["environments"][0]["last_received_seconds_ago"] is None


def test_duplicate_environment_and_scalar_samples_do_not_choose_first():
    store = FakeStore(results={"timestamp(up": [vec("aws", 1), vec("aws", 99)]})
    for path in ("", "/aws/http", "/aws/database"):
        response = client(store).get("/v1/observability/environments"+path, headers=AUTH)
        assert response.status_code == 503
        assert response.json() == {"detail": "store_unavailable"}


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf", -1])
def test_http_invalid_metric_is_null_not_success_or_500(value):
    store = FakeStore(results={"timestamp(up": [vec("aws", 2)], "sum(rate": [vec("aws", value)]})
    response = client(store).get("/v1/observability/environments/aws/http", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["requests_per_second"] is None
    assert response.json()["state"] == "UNOBSERVABLE"


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf", 2, -1])
def test_invalid_db_boolean_is_unknown_not_down(value):
    store = FakeStore(results={"timestamp(up": [vec("aws", 2)], "cnpg_collector_up": [{"metric": {"cnpg_cluster": "source-db", "pod": "source-db-1", "instance_role": "primary"}, "value": [0, str(value)]}]})
    response = client(store).get("/v1/observability/environments/aws/database", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["instances"][0]["up"] is None
    assert response.json()["state"] == "UNOBSERVABLE"
    assert "timestamp(cnpg_collector_up" in store.seen[1]  # a live Collector cannot bless stale DB data


@pytest.mark.parametrize("body", [[], None, {}, {"status":"success","data":[]}, {"status":"success","data":{"resultType":"matrix","result":[]}}, {"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[0,"not-a-number"]}]}}, {"status":"success","data":{"resultType":"vector","result":[{"metric":None,"value":[0,"1"]}]}}])
def test_malformed_store_response_is_redacted_503(body):
    class Malformed:
        def handler(self, _request):
            return httpx.Response(200, json=body)
    response = client(Malformed()).get("/v1/observability/environments", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "store_unavailable"}


def test_response_size_limit():
    class Oversize:
        def handler(self, _request):
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
    response = client(Oversize()).get("/v1/observability/environments", headers=AUTH)
    assert response.status_code == 503


@pytest.mark.parametrize("token", ["", "short", "x"*513, "x"*32+" ", "한"*32])
def test_token_configuration_rejects_weak_or_invalid_values(token):
    with pytest.raises(ValueError, match="invalid observability API token"):
        bearer_token(token)


def test_success_and_rejection_are_not_cacheable():
    c = client(FakeStore())
    for headers in ({}, AUTH):
        response = c.get("/v1/observability/environments", headers=headers)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_foreign_environment_alerts_are_not_returned():
    store = FakeStore(alerts=[{"labels":{"alertname":"BravoEnvUnobservable","bravo_env":"foreign"},"state":"firing","activeAt":"2026-11-04T00:00:00Z"}])
    response = client(store).get("/v1/observability/alerts", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["alerts"] == []


@pytest.mark.parametrize("labels", [{}, {"cnpg_cluster": "source-db"}, {"cnpg_cluster": "source-db", "pod": "source-db-1"}, {"cnpg_cluster": "source-db", "pod": "source-db-1", "instance_role": "unknown"}])
def test_database_identity_missing_cannot_be_promoted_to_ok(labels):
    store = FakeStore(results={"timestamp(up": [vec("aws", 2)], "cnpg_collector_up": [{"metric": labels, "value": [0, "1"]}]})
    body = client(store).get("/v1/observability/environments/aws/database", headers=AUTH).json()
    assert body["state"] == "UNOBSERVABLE" and body["instances"] == []
    assert all('bravo_signal="cnpg-scrape"' in q and 'cnpg_cluster!=""' in q for q in store.seen[1:])
    assert 'pod!=""' in store.seen[1] and 'instance_role=~"primary|replica"' in store.seen[1]
