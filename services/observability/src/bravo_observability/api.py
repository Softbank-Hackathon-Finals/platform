"""콘솔용 관측 조회 API (T37). 정해 둔 조회식만 실행하고 임의 PromQL은 받지 않는다.

지표 저장소에는 mTLS 클라이언트 인증서로 붙는다. 저장소에 닿지 못하거나 값이 없으면 0이 아니라
"확인 불가"(state UNOBSERVABLE, 값 null)로 돌려준다. 이 API의 값은 판정 입력이 아니다(ADR-0010).
"""

import hmac
import os
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Request

STALE_AFTER_SECONDS = 30
ENV_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HTTP_METRIC = "http_server_request_duration_seconds"

Authorizer = Callable[[Request], bool]


def deny_all(_request: Request) -> bool:
    return False


def bearer_token(token: str) -> Authorizer:
    expected = f"Bearer {token}".encode()

    def authorize(request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), expected)

    return authorize


class StoreUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Prometheus:
    """지표 저장소 즉시 조회. 실패는 모두 StoreUnavailable로 바꾼다."""

    client: httpx.Client

    def query(self, promql: str) -> list[dict]:
        try:
            r = self.client.get("/api/v1/query", params={"query": promql})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise StoreUnavailable(type(e).__name__) from e
        if body.get("status") != "success":
            raise StoreUnavailable("query_failed")
        return body["data"]["result"]

    def alerts(self) -> list[dict]:
        try:
            r = self.client.get("/api/v1/alerts")
            r.raise_for_status()
            return r.json()["data"]["alerts"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise StoreUnavailable(type(e).__name__) from e


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scalar(result: list[dict]) -> float | None:
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else value  # NaN(0으로 나눔)은 값 없음


def create_app(
    store: Prometheus, environments: list[str], *, authorize: Authorizer = deny_all
) -> FastAPI:
    bad = [e for e in environments if not ENV_NAME.fullmatch(e)]
    if bad or not environments or len(set(environments)) != len(environments):
        raise ValueError(f"invalid environment list: {environments}")
    registered = tuple(environments)
    app = FastAPI(title="T37 observability query API", docs_url=None, redoc_url=None, openapi_url=None)

    def guard(request: Request) -> None:
        if authorize(request) is not True:
            raise HTTPException(401, "unauthorized")

    def env_or_404(env: str) -> str:
        if env not in registered:
            raise HTTPException(404, "environment_not_registered")
        return env

    def run(fn):
        try:
            return fn()
        except StoreUnavailable as e:
            raise HTTPException(503, "store_unavailable") from e

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/v1/observability/environments")
    def environments_view(request: Request):
        guard(request)
        rows = run(
            lambda: store.query(
                'time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))'
            )
        )
        ages = {r["metric"].get("bravo_env"): float(r["value"][1]) for r in rows}
        out = []
        for env in registered:
            age = ages.get(env)
            ok = age is not None and age <= STALE_AFTER_SECONDS
            out.append(
                {
                    "environment": env,
                    "state": "OK" if ok else "UNOBSERVABLE",
                    "last_received_seconds_ago": None if age is None else round(age, 1),
                }
            )
        return {"queried_at": _now(), "stale_after_seconds": STALE_AFTER_SECONDS, "environments": out}

    @app.get("/v1/observability/alerts")
    def alerts_view(request: Request):
        guard(request)
        rows = run(store.alerts)
        alerts = [
            {
                "alertname": a["labels"].get("alertname"),
                "state": a.get("state"),
                "environment": a["labels"].get("bravo_env"),
                "severity": a["labels"].get("severity"),
                "summary": a.get("annotations", {}).get("summary"),
                "runbook": a.get("annotations", {}).get("runbook"),
                "active_at": a.get("activeAt"),
            }
            for a in rows
            if str(a.get("labels", {}).get("alertname", "")).startswith("Bravo")
        ]
        alerts.sort(key=lambda a: (a["alertname"] or "", a["environment"] or ""))
        return {"queried_at": _now(), "alerts": alerts}

    @app.get("/v1/observability/environments/{env}/http")
    def http_view(env: str, request: Request):
        guard(request)
        e = env_or_404(env)
        sel = f'bravo_env="{e}"'
        count = f"{HTTP_METRIC}_count"
        queries = {
            "requests_per_second": f"sum(rate({count}{{{sel}}}[1m]))",
            "error_ratio": f'sum(rate({count}{{{sel}, http_response_status_code=~"5.."}}[1m])) / sum(rate({count}{{{sel}}}[1m]))',
            "p95_seconds": f"histogram_quantile(0.95, sum by (le) (rate({HTTP_METRIC}_bucket{{{sel}}}[1m])))",
        }
        values = run(lambda: {k: _scalar(store.query(q)) for k, q in queries.items()})
        # 요청이 하나도 없으면 오류율 · p95는 계산할 수 없다(0이 아니다)
        if not values["requests_per_second"]:
            values["error_ratio"] = values["p95_seconds"] = None
        return {"queried_at": _now(), "environment": e, "window": "1m", **values}

    @app.get("/v1/observability/environments/{env}/database")
    def database_view(env: str, request: Request):
        guard(request)
        e = env_or_404(env)
        sel = f'bravo_env="{e}"'
        up, lag, active = run(
            lambda: (
                store.query(f"max by (cnpg_cluster, pod, instance_role) (cnpg_collector_up{{{sel}}})"),
                store.query(
                    f"max by (cnpg_cluster, slot_name, slot_type) (cnpg_pg_replication_slots_pg_wal_lsn_diff{{{sel}}})"
                ),
                store.query(f"max by (cnpg_cluster, slot_name) (cnpg_pg_replication_slots_active{{{sel}}})"),
            )
        )
        is_active = {(r["metric"].get("cnpg_cluster"), r["metric"].get("slot_name")): float(r["value"][1]) == 1 for r in active}
        instances = sorted(
            (
                {
                    "cluster": r["metric"].get("cnpg_cluster"),
                    "pod": r["metric"].get("pod"),
                    "role": r["metric"].get("instance_role"),
                    "up": float(r["value"][1]) == 1,
                }
                for r in up
            ),
            key=lambda i: (i["cluster"] or "", i["pod"] or ""),
        )
        slots = sorted(
            (
                {
                    "cluster": r["metric"].get("cnpg_cluster"),
                    "slot": r["metric"].get("slot_name"),
                    "type": r["metric"].get("slot_type"),
                    "lag_bytes": float(r["value"][1]),
                    "active": is_active.get((r["metric"].get("cnpg_cluster"), r["metric"].get("slot_name"))),
                }
                for r in lag
            ),
            key=lambda s: (s["cluster"] or "", s["slot"] or ""),
        )
        # 인스턴스 지표가 하나도 없으면 "DB 없음"이 아니라 확인 불가다
        state = "UNOBSERVABLE" if not instances else "OK" if all(i["up"] for i in instances) else "DOWN"
        return {"queried_at": _now(), "environment": e, "state": state, "instances": instances, "replication_slots": slots}

    return app


def from_env() -> FastAPI:
    """배포용 진입점. 설정이 하나라도 없으면 시작하지 않는다."""
    token = os.environ["BRAVO_OBSERVABILITY_API_TOKEN"]
    if len(token) < 32:
        raise ValueError("BRAVO_OBSERVABILITY_API_TOKEN must be at least 32 characters")
    tls = os.environ.get("BRAVO_TLS_DIR", "/etc/bravo-tls")
    ctx = ssl.create_default_context(cafile=f"{tls}/ca.crt")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(f"{tls}/tls.crt", f"{tls}/tls.key")
    client = httpx.Client(base_url=os.environ["PROMETHEUS_URL"], verify=ctx, timeout=3.0)
    envs = [e.strip() for e in os.environ["BRAVO_ENVIRONMENTS"].split(",") if e.strip()]
    return create_app(Prometheus(client), envs, authorize=bearer_token(token))


def main() -> None:
    import uvicorn

    uvicorn.run(from_env(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)
