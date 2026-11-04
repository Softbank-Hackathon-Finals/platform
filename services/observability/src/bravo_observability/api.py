"""콘솔용 관측 조회 API (T37). 정해 둔 조회식만 실행하고 임의 PromQL은 받지 않는다.

지표 저장소에는 mTLS 클라이언트 인증서로 붙는다. 저장소에 닿지 못하거나 값이 없으면 0이 아니라
"확인 불가"(state UNOBSERVABLE, 값 null)로 돌려준다. 이 API의 값은 판정 입력이 아니다(ADR-0010).
"""

import hmac
import json
import math
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
# DB 지표는 Collector의 DB 긁기 경로(collector-db.yaml)가 붙이는 출처 라벨로만 고른다. 앱 OTLP 경로는 이 라벨을 지우므로
# 앱이 같은 이름(cnpg_collector_up 등)의 지표를 보내도 DB 상태에 들어오지 않는다.
DB_SIGNAL = 'bravo_signal="cnpg-scrape"'
# PgBouncer 지표도 같은 방식으로 PgBouncer 긁기 경로(components/pgbouncer)의 출처 라벨로만 고른다.
PGBOUNCER_SIGNAL = 'bravo_signal="pgbouncer-scrape"'
MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_STORE_ROWS = 10_000

Authorizer = Callable[[Request], bool]


def deny_all(_request: Request) -> bool:
    return False


def bearer_token(token: str) -> Authorizer:
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("invalid observability API token")
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

    def _read(self, path: str, *, params=None) -> dict:
        try:
            with self.client.stream("GET", path, params=params) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_STORE_BYTES:
                        raise StoreUnavailable("response_too_large")
                    raw.extend(chunk)
                body = json.loads(raw)
            if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(body.get("data"), dict):
                raise StoreUnavailable("invalid_response")
            return body["data"]
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise StoreUnavailable("store_request_failed") from exc

    def query(self, promql: str) -> list[dict]:
        data = self._read("/api/v1/query", params={"query": promql})
        rows = data.get("result")
        if data.get("resultType") != "vector" or not isinstance(rows, list) or len(rows) > MAX_STORE_ROWS:
            raise StoreUnavailable("invalid_vector")
        for row in rows:
            if not isinstance(row, dict) or not _labels(row.get("metric")):
                raise StoreUnavailable("invalid_sample")
            value = row.get("value")
            if not isinstance(value, list) or len(value) != 2 or type(value[0]) not in (int, float) or not math.isfinite(value[0]) or not isinstance(value[1], str):
                raise StoreUnavailable("invalid_sample")
            _number(row)  # reject invalid numeric encoding before any output
        return rows

    def alerts(self) -> list[dict]:
        rows = self._read("/api/v1/alerts").get("alerts")
        if not isinstance(rows, list) or len(rows) > MAX_STORE_ROWS:
            raise StoreUnavailable("invalid_alerts")
        for row in rows:
            if not isinstance(row, dict) or not _labels(row.get("labels")) or not _labels(row.get("annotations", {})):
                raise StoreUnavailable("invalid_alert")
            if row.get("state") not in ("pending", "firing") or not isinstance(row.get("activeAt"), str):
                raise StoreUnavailable("invalid_alert")
        return rows


def _labels(value) -> bool:
    return isinstance(value, dict) and len(value) <= 128 and all(isinstance(k, str) and isinstance(v, str) and len(k) <= 256 and len(v) <= 8192 for k, v in value.items())


def _number(row: dict) -> float | None:
    try:
        value = float(row["value"][1])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise StoreUnavailable("invalid_number") from exc
    return value if math.isfinite(value) else None


def _boolean(row: dict) -> bool | None:
    value = _number(row)
    return bool(value) if value in (0.0, 1.0) else None


def _heartbeat_query(env: str | None = None) -> str:
    selector = 'bravo_signal="collector-self"'
    if env is not None:
        selector += f',bravo_env="{env}"'
    metric = f"up{{{selector}}}"
    return f"time() - max by (bravo_env) (timestamp({metric}) and ({metric} == 1))"


def _fresh_series(name: str, selector: str) -> str:
    metric = f"{name}{{{selector}}}"
    age = f"time() - timestamp({metric})"
    return f"({metric} and ({age} >= 0) and ({age} <= {STALE_AFTER_SECONDS}))"


def _identified(row: dict, *labels: str) -> bool:
    """CNPG 식별자가 모두 있는 행만 상태에 쓴다. 식별자 없는 행을 정상 인스턴스로 올리지 않는다."""
    return all(isinstance(v := row["metric"].get(k), str) and v for k in labels)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scalar(result: list[dict]) -> float | None:
    if not result:
        return None
    if len(result) != 1:
        raise StoreUnavailable("ambiguous_scalar")
    return _number(result[0])


def create_app(
    store: Prometheus, environments: list[str], *, authorize: Authorizer = deny_all
) -> FastAPI:
    bad = [e for e in environments if not ENV_NAME.fullmatch(e)]
    if bad or not environments or len(set(environments)) != len(environments):
        raise ValueError(f"invalid environment list: {environments}")
    registered = tuple(environments)
    app = FastAPI(title="T37 observability query API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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
        rows = run(lambda: store.query(_heartbeat_query()))
        ages = {}
        for row in rows:
            env = row["metric"].get("bravo_env")
            if env not in registered:
                continue
            if env in ages:
                raise HTTPException(503, "store_unavailable")
            age = _number(row)
            ages[env] = age if age is not None and age >= 0 else None
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
            if str(a.get("labels", {}).get("alertname", "")).startswith("Bravo") and a["labels"].get("bravo_env") in registered
        ]
        alerts.sort(key=lambda a: (a["alertname"] or "", a["environment"] or ""))
        return {"queried_at": _now(), "alerts": alerts}

    def freshness(env: str) -> tuple[bool, float | None]:
        age = run(lambda: _scalar(store.query(_heartbeat_query(env))))
        if age is None or age < 0:
            return False, None
        return age <= STALE_AFTER_SECONDS, round(age, 1)

    @app.get("/v1/observability/environments/{env}/http")
    def http_view(env: str, request: Request):
        guard(request)
        e = env_or_404(env)
        sel = f'bravo_env="{e}"'
        count = f"{HTTP_METRIC}_count"
        total = f"sum(rate({count}{{{sel}}}[1m]))"
        errors = f'sum(rate({count}{{{sel}, http_response_status_code=~"5.."}}[1m]))'
        queries = {
            "requests_per_second": total,
            # No 5xx series is normal when every recorded response succeeded.
            # Derive zero only from an existing denominator; absent telemetry
            # stays absent, and an idle denominator yields NaN -> null below.
            "error_ratio": f"({errors} or (0 * {total})) / {total}",
            "p95_seconds": f"histogram_quantile(0.95, sum by (le) (rate({HTTP_METRIC}_bucket{{{sel}}}[1m])))",
        }
        fresh, age = freshness(e)
        values = run(lambda: {k: _scalar(store.query(q)) for k, q in queries.items()}) if fresh else dict.fromkeys(queries)
        for key, value in values.items():
            if value is not None and (value < 0 or (key == "error_ratio" and value > 1)):
                values[key] = None
        # 요청이 하나도 없으면 오류율 · p95는 계산할 수 없다(0이 아니다)
        if not values["requests_per_second"]:
            values["error_ratio"] = values["p95_seconds"] = None
        return {"queried_at": _now(), "environment": e, "window": "1m", "state": "OK" if fresh and values["requests_per_second"] is not None else "UNOBSERVABLE", "last_received_seconds_ago": age, **values}

    @app.get("/v1/observability/environments/{env}/database")
    def database_view(env: str, request: Request):
        guard(request)
        e = env_or_404(env)
        sel = f'bravo_env="{e}",{DB_SIGNAL}'
        fresh, age = freshness(e)
        if not fresh:
            return {"queried_at": _now(), "environment": e, "state": "UNOBSERVABLE", "last_received_seconds_ago": age, "instances": [], "replication_slots": []}
        up, lag, active = run(
            lambda: (
                store.query(f"max by (cnpg_cluster, pod, instance_role) ({_fresh_series('cnpg_collector_up', sel)})"),
                store.query(
                    f"max by (cnpg_cluster, slot_name, slot_type) ({_fresh_series('cnpg_pg_replication_slots_pg_wal_lsn_diff', sel)})"
                ),
                store.query(f"max by (cnpg_cluster, slot_name) ({_fresh_series('cnpg_pg_replication_slots_active', sel)})"),
            )
        )
        is_active = {(r["metric"].get("cnpg_cluster"), r["metric"].get("slot_name")): _boolean(r) for r in active}
        instances = sorted(
            (
                {
                    "cluster": r["metric"].get("cnpg_cluster"),
                    "pod": r["metric"].get("pod"),
                    "role": r["metric"].get("instance_role"),
                    "up": _boolean(r) if _identified(r, "cnpg_cluster", "pod") else None,
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
                    "lag_bytes": value if (value := _number(r)) is not None and value >= 0 and _identified(r, "cnpg_cluster", "slot_name") else None,
                    "active": is_active.get((r["metric"].get("cnpg_cluster"), r["metric"].get("slot_name"))),
                }
                for r in lag
            ),
            key=lambda s: (s["cluster"] or "", s["slot"] or ""),
        )
        # 인스턴스 지표가 하나도 없으면 "DB 없음"이 아니라 확인 불가다
        state = "UNOBSERVABLE" if not instances or any(i["up"] is None for i in instances) else "OK" if all(i["up"] for i in instances) else "DOWN"
        return {"queried_at": _now(), "environment": e, "state": state, "last_received_seconds_ago": age, "instances": instances, "replication_slots": slots}

    @app.get("/v1/observability/environments/{env}/pgbouncer")
    def pgbouncer_view(env: str, request: Request):
        guard(request)
        e = env_or_404(env)
        sel = f'bravo_env="{e}",{PGBOUNCER_SIGNAL}'
        fresh, age = freshness(e)
        if not fresh:
            return {"queried_at": _now(), "environment": e, "state": "UNOBSERVABLE", "last_received_seconds_ago": age, "poolers": []}
        # PgBouncer 관리 DB(pgbouncer)는 업무 DB가 아니므로 뺀다
        dbs, pools = f'{sel},name!="pgbouncer"', f'{sel},database!="pgbouncer"'
        up, paused, active, waiting, servers = run(
            lambda: (
                store.query(f"max by (pooler) ({_fresh_series('pgbouncer_up', sel)})"),
                store.query(f"max by (pooler, name) ({_fresh_series('pgbouncer_databases_paused', dbs)})"),
                store.query(f"sum by (pooler, database) ({_fresh_series('pgbouncer_pools_client_active_connections', pools)})"),
                store.query(f"sum by (pooler, database) ({_fresh_series('pgbouncer_pools_client_waiting_connections', pools)})"),
                store.query(f"sum by (pooler, database) ({_fresh_series('pgbouncer_pools_server_active_connections', pools)})"),
            )
        )

        def counts(rows):
            return {
                (r["metric"].get("pooler"), r["metric"].get("database")): value if (value := _number(r)) is not None and value >= 0 else None
                for r in rows
                if _identified(r, "pooler", "database")
            }

        clients, queued, backends = counts(active), counts(waiting), counts(servers)
        poolers = {
            r["metric"].get("pooler"): {"pooler": r["metric"].get("pooler"), "up": _boolean(r) if _identified(r, "pooler") else None, "databases": []}
            for r in up
        }
        for r in sorted(paused, key=lambda r: (r["metric"].get("pooler") or "", r["metric"].get("name") or "")):
            key = (r["metric"].get("pooler"), r["metric"].get("name"))
            # 접속 지표 없이 DB 행만 있는 pooler는 확인할 수 없는 상태다
            target = poolers.setdefault(key[0], {"pooler": key[0], "up": None, "databases": []})
            target["databases"].append(
                {
                    "name": key[1],
                    "paused": _boolean(r) if _identified(r, "pooler", "name") else None,
                    "clients_active": clients.get(key),
                    "clients_waiting": queued.get(key),
                    "servers_active": backends.get(key),
                }
            )
        rows = sorted(poolers.values(), key=lambda x: x["pooler"] or "")
        flags = [d["paused"] for x in rows for d in x["databases"]]
        # PAUSED는 컷오버 중의 정상 상태일 수 있다. 판정 · 실행은 이 값을 쓰지 않는다(Executor가 PgBouncer에서 직접 읽는다)
        if not rows or any(x["up"] is None for x in rows) or any(f is None for f in flags):
            state = "UNOBSERVABLE"
        elif not all(x["up"] for x in rows):
            state = "DOWN"
        else:
            state = "PAUSED" if any(flags) else "OK"
        return {"queried_at": _now(), "environment": e, "state": state, "last_received_seconds_ago": age, "poolers": rows}

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
    client = httpx.Client(base_url=os.environ["PROMETHEUS_URL"], verify=ctx, timeout=3.0, trust_env=False)
    envs = [e.strip() for e in os.environ["BRAVO_ENVIRONMENTS"].split(",") if e.strip()]
    return create_app(Prometheus(client), envs, authorize=bearer_token(token))


def main() -> None:
    import uvicorn

    uvicorn.run(from_env(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)
