"""T18 부하 생성기의 창 요약 수신 API (T36 계약 3절 · 6절).

콘솔 조회 API와 다른 프로세스 · 다른 주체다. 지표 저장소에 붙지 않고 밖으로 나가는 연결이 없다.
판정은 여기서 하지 않는다. 저장한 창은 판정(T17)이 WindowStore로 읽는다.
"""

import json
import os
import re
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from bravo_observability.api import Authorizer, _now, bearer_token, deny_all
from bravo_observability.intake import IntakeRejected, SqliteWindowStore, WindowStore, accept

MAX_WINDOW_BYTES = 64 * 1024
STATUS = {
    "invalid_window": 422,
    "clock_ahead": 422,
    "plan_not_accepting_windows": 404,
    "duplicate": 409,
    "out_of_order": 409,
}
PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # 계약 스키마의 Identifier


def create_intake_app(
    windows: WindowStore,
    *,
    authorize: Authorizer = deny_all,
    is_active_plan: Callable[[str], bool] = lambda _plan_id: False,
    clock: Callable[[], str] = _now,
) -> FastAPI:
    app = FastAPI(title="T36 SLI window intake", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/v1/observability/windows", status_code=201)
    async def receive_window(request: Request):
        if authorize(request) is not True:
            raise HTTPException(401, "unauthorized")
        # 끝까지 읽은 뒤가 아니라 읽는 중에 자른다
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_WINDOW_BYTES:
                raise HTTPException(413, "window_too_large")
        try:
            document = json.loads(body)
        except ValueError as e:
            raise HTTPException(400, "invalid_json") from e
        try:
            stored = await run_in_threadpool(
                accept, windows, document, now=clock(), is_active_plan=is_active_plan
            )
        except IntakeRejected as e:
            raise HTTPException(STATUS[e.code], e.code) from e
        return {
            "plan_id": stored["plan_id"],
            "window_start": stored["window"]["start"],
            "received_at": stored["received_at"],
        }

    return app


def from_env() -> FastAPI:
    """배포용 진입점. 설정이 하나라도 없거나 틀리면 시작하지 않는다."""
    token = os.environ["BRAVO_WINDOW_TOKEN"]
    if len(token) < 32:
        raise ValueError("BRAVO_WINDOW_TOKEN must be at least 32 characters")
    # 받을 계획 목록. T10 상태 DB에 연결하기 전까지 배포 설정으로 명시한다
    plans = [p.strip() for p in os.environ["BRAVO_WINDOW_PLANS"].split(",") if p.strip()]
    if not plans or not all(PLAN_ID.fullmatch(p) for p in plans):
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
