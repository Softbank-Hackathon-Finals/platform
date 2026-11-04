"""T18 부하 생성기의 창 요약 수신 API (T36 계약 3절 · 6절).

콘솔 조회 API와 다른 프로세스 · 다른 주체다. 지표 저장소에 붙지 않고 밖으로 나가는 연결이 없다.
판정은 여기서 하지 않는다. 저장한 창은 판정(T17)이 WindowStore로 읽는다.
"""

import asyncio
import json
import math
import os
import re
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from bravo_observability.api import Authorizer, _now, bearer_token, deny_all
from bravo_observability.intake import IntakeRejected, SqliteWindowStore, WindowStore, accept

MAX_WINDOW_BYTES = 64 * 1024
MAX_BODY_SECONDS = 5
STATUS = {
    "invalid_window": 422,
    "clock_ahead": 422,
    "plan_not_accepting_windows": 404,
    "duplicate": 409,
    "out_of_order": 409,
    "overlapping_window": 409,
}
PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # 계약 스키마의 Identifier


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member")
        result[key] = value
    return result


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_json_number")
    return number


def _constant(_value):
    raise ValueError("invalid_json_constant")


def _bounded_structure(document):
    pending = [(document, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ValueError("json_structure_limit")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def create_intake_app(
    windows: WindowStore,
    *,
    authorize: Authorizer = deny_all,
    is_active_plan: Callable[[str], bool] = lambda _plan_id: False,
    clock: Callable[[], str] = _now,
) -> FastAPI:
    app = FastAPI(title="T36 SLI window intake", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def guard(request):
        try:
            allowed = authorize(request)
        except Exception:
            raise HTTPException(503, "authority_unavailable") from None
        if allowed is not True:
            raise HTTPException(401, "unauthorized")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/v1/observability/windows", status_code=201)
    async def receive_window(request: Request):
        guard(request)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "encoded_window_not_supported")
        # 끝까지 읽은 뒤가 아니라 읽는 중에 자른다
        body = bytearray()
        try:
            async with asyncio.timeout(MAX_BODY_SECONDS):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_WINDOW_BYTES:
                        raise HTTPException(413, "window_too_large")
                    body.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "window_read_timeout") from None
        except ClientDisconnect:
            raise HTTPException(400, "window_disconnected") from None
        try:
            document = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant, parse_float=_finite)
            _bounded_structure(document)
        except (ValueError, RecursionError) as e:
            raise HTTPException(400, "invalid_json") from e

        def persist():
            # Do not keep an authorization or clock value from before a slow
            # body upload/thread-pool wait. The host owns producer identity and
            # the active-plan registry; the caller cannot supply either.
            guard(request)
            return accept(windows, document, now=clock(), is_active_plan=is_active_plan)

        try:
            stored = await run_in_threadpool(persist)
        except IntakeRejected as e:
            raise HTTPException(STATUS.get(e.code, 503), e.code if e.code in STATUS else "window_store_unavailable") from None
        except HTTPException:
            raise
        except Exception:
            # A commit may already have succeeded. Do not fabricate a 201 or
            # repeat the write internally; the same window key is replay-safe.
            raise HTTPException(503, "window_store_unavailable") from None
        return {
            "plan_id": stored["plan_id"],
            "window_start": stored["window"]["start"],
            "received_at": stored["received_at"],
        }

    return app


def from_env() -> FastAPI:
    """배포용 진입점. 설정이 하나라도 없거나 틀리면 시작하지 않는다."""
    token = os.environ["BRAVO_WINDOW_TOKEN"]
    if not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("BRAVO_WINDOW_TOKEN must be bounded non-whitespace ASCII")
    # 받을 계획 목록. T10 상태 DB에 연결하기 전까지 배포 설정으로 명시한다
    plans = [p.strip() for p in os.environ["BRAVO_WINDOW_PLANS"].split(",") if p.strip()]
    if not plans or len(plans) > 256 or len(set(plans)) != len(plans) or not all(PLAN_ID.fullmatch(p) for p in plans):
        raise ValueError("BRAVO_WINDOW_PLANS must be a comma-separated list of plan ids")
    accepting = frozenset(plans)
    return create_intake_app(
        SqliteWindowStore(os.environ["BRAVO_WINDOW_DB"]),
        authorize=bearer_token(token),
        is_active_plan=lambda plan_id: plan_id in accepting,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(from_env(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)
