"""상태 머신(T10)이 부르는 HTTP API (spec D1: 202 + 상태 조회).

- POST /v1/cutovers: Idempotency-Key 필수. 새 실행이면 202, 같은 키 재요청이면 저장된 실행을 200으로 돌려준다.
- GET /v1/cutovers/{execution_id}, GET /v1/cutovers?plan_id=&epoch=: 응답을 잃었을 때 같은 호출을
  반복하지 않고 상태를 다시 읽는 경로.
"""

import os
import secrets
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.requests import Request

from bravo_cutover.contract import CutoverRequest
from bravo_cutover.rejections import Rejected
from bravo_cutover.resources import RegisteredPair, Registry
from bravo_cutover.store import Store


class Runner(Protocol):
    def submit(self, execution_id: UUID) -> None: ...


class NoRunner:
    """읽기 전용 앱. 실제 실행기와 자원 등록이 없으면 POST를 거부한다."""

    def submit(self, execution_id: UUID) -> None:
        return None


def create_app(store: Store | None = None, runner: Runner | None = None,
               registry: Registry | None = None, *, api_token: str | None = None,
               migrate: bool = False) -> FastAPI:
    """EXECUTOR_DB(실행 기록 DB), EXECUTOR_CONFIG(환경 설정 파일)로 실제 실행기를 만든다.
    자원 등록은 EXECUTOR_CONFIG의 환경 목록에서 만들고, 설정 파일 없이 기록만 남길 때는 EXECUTOR_RESOURCES를 읽는다."""
    if store is None:
        store = Store(os.environ["EXECUTOR_DB"])
        if migrate:
            store.migrate()
    if (runner is None or registry is None) and os.environ.get("EXECUTOR_CONFIG"):
        from bravo_cutover.runner import Cutover, ThreadRunner
        from bravo_cutover.sites import load_config, registry_from_sites

        config = load_config(os.environ["EXECUTOR_CONFIG"])
        registry = registry or registry_from_sites(config.sites)
        runner = runner or ThreadRunner(Cutover(store, config), store)
    if registry is None and os.environ.get("EXECUTOR_RESOURCES"):
        registry = Registry.from_file(os.environ["EXECUTOR_RESOURCES"])
    runner = runner or NoRunner()
    app = FastAPI(title="cutover-executor")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if api_token is not None and request.url.path.startswith("/v1/"):
            provided = request.headers.get("authorization", "")
            if not secrets.compare_digest(provided.encode(), ("Bearer " + api_token).encode()):
                return JSONResponse({"detail": {"code": "unauthorized"}}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/resources", response_model=RegisteredPair)
    def get_resources():
        pair = registry.public_pair() if registry is not None and api_token else None
        if pair is None:
            raise HTTPException(503, {"code": "executor_not_configured"})
        return pair

    @app.post("/v1/cutovers")
    def start_cutover(
        request: CutoverRequest,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        if not idempotency_key:
            raise HTTPException(400, {"code": "idempotency_key_required"})
        if registry is None or isinstance(runner, NoRunner):
            raise HTTPException(503, {"code": "executor_not_configured"})
        try:
            # 요청의 환경 · 접속 참조를 서버 등록 정보로 실제 DB 자원에 결속한 뒤 판정한다
            execution, created = store.create(idempotency_key, request, registry.resolve(request))
        except Rejected as e:
            raise HTTPException(e.http_status, {"code": e.code, "message": str(e)}) from e
        if created:
            runner.submit(execution.execution_id)
        return JSONResponse(execution.view(), status_code=202 if created else 200)

    @app.get("/v1/cutovers/{execution_id}")
    def get_cutover(execution_id: UUID):
        execution = store.get(execution_id)
        if execution is None:
            raise HTTPException(404, {"code": "execution_not_found"})
        return execution.view()

    @app.get("/v1/cutovers")
    def find_cutovers(plan_id: Annotated[str, Query(max_length=128)], epoch: Annotated[int, Query(ge=1)]):
        return [e.view() for e in store.find(plan_id, epoch)]

    return app
