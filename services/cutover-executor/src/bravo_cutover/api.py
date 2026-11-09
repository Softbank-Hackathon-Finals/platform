"""상태 머신(T10)이 부르는 HTTP API (spec D1: 202 + 상태 조회).

- POST /v1/cutovers: Idempotency-Key 필수. 새 실행이면 202, 같은 키 재요청이면 저장된 실행을 200으로 돌려준다.
- GET /v1/cutovers/{execution_id}, GET /v1/cutovers?plan_id=&epoch=: 응답을 잃었을 때 같은 호출을
  반복하지 않고 상태를 다시 읽는 경로.
- POST /v1/readiness: 컷오버 전 데이터 준비 관찰(T19 게이트 입력, readiness.py). 목적지 시퀀스를 올리기만 하므로
  명령 자격이 필요하고, 끝나지 않은 실행이 있으면 거부한다.
"""

import os
import secrets
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.datastructures import Headers

from bravo_cutover.contract import CutoverRequest
from bravo_cutover.readiness import NotObservable, ReadinessRequest
from bravo_cutover.rejections import Rejected
from bravo_cutover.resources import RegisteredPair, Registry
from bravo_cutover.store import Store


class Runner(Protocol):
    def submit(self, execution_id: UUID) -> None: ...


class Observer(Protocol):
    def observe(self, request: ReadinessRequest) -> dict: ...


class NoRunner:
    """읽기 전용 앱. 실제 실행기와 자원 등록이 없으면 POST를 거부한다."""

    def submit(self, execution_id: UUID) -> None:
        return None


def validate_credentials(read_token: str | None, command_token: str | None) -> None:
    if command_token is not None and (not read_token or not command_token or read_token == command_token):
        raise ValueError("executor command credential requires a distinct read credential")


def create_app(store: Store | None = None, runner: Runner | None = None,
               registry: Registry | None = None, *, api_token: str | None = None,
               command_token: str | None = None, migrate: bool = False,
               readiness: Observer | None = None) -> FastAPI:
    """EXECUTOR_DB(실행 기록 DB), EXECUTOR_CONFIG(환경 설정 파일)로 실제 실행기를 만든다.
    자원 등록은 EXECUTOR_CONFIG의 환경 목록에서 만들고, 설정 파일 없이 기록만 남길 때는 EXECUTOR_RESOURCES를 읽는다."""
    validate_credentials(api_token, command_token)
    if store is None:
        store = Store(os.environ["EXECUTOR_DB"])
        if migrate:
            store.migrate()
    if (runner is None or registry is None) and os.environ.get("EXECUTOR_CONFIG"):
        from bravo_cutover.readiness import Readiness
        from bravo_cutover.runner import Cutover, ThreadRunner
        from bravo_cutover.sites import load_config, registry_from_sites

        config = load_config(os.environ["EXECUTOR_CONFIG"])
        registry = registry or registry_from_sites(config.sites, origin=config.origin)
        runner = runner or ThreadRunner(Cutover(store, config), store)
        readiness = readiness or Readiness(config)
    if registry is None and os.environ.get("EXECUTOR_RESOURCES"):
        registry = Registry.from_file(os.environ["EXECUTOR_RESOURCES"])
    runner = runner or NoRunner()
    app = FastAPI(title="cutover-executor")

    class AuthenticatedRoute(APIRoute):
        # Authenticate the matched route before FastAPI consumes the request body.
        async def handle(self, scope, receive, send):
            if not api_token:
                raise HTTPException(503, {"code": "executor_not_configured"})
            provided = Headers(scope=scope).get("authorization", "").encode()
            is_command = command_token and secrets.compare_digest(provided, ("Bearer " + command_token).encode())
            if not is_command:
                if not secrets.compare_digest(provided, ("Bearer " + api_token).encode()):
                    raise HTTPException(401, {"code": "unauthorized"})
                if scope["method"] != "GET":
                    raise HTTPException(403, {"code": "command_credential_required"})
            await super().handle(scope, receive, send)

    api = APIRouter(prefix="/v1", route_class=AuthenticatedRoute)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @api.get("/resources", response_model=RegisteredPair)
    def get_resources():
        pair = registry.public_pair() if registry is not None and api_token else None
        if pair is None:
            raise HTTPException(503, {"code": "executor_not_configured"})
        return pair

    @api.post("/cutovers")
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

    @api.get("/cutovers/{execution_id}")
    def get_cutover(execution_id: UUID):
        execution = store.get(execution_id)
        if execution is None:
            raise HTTPException(404, {"code": "execution_not_found"})
        return execution.view()

    @api.get("/cutovers")
    def find_cutovers(plan_id: Annotated[str, Query(max_length=128)], epoch: Annotated[int, Query(ge=1)]):
        return [e.view() for e in store.find(plan_id, epoch)]

    @api.post("/readiness")
    def observe_readiness(request: ReadinessRequest):
        if readiness is None:
            raise HTTPException(503, {"code": "executor_not_configured"})
        # 실행 중에는 그 실행만 목적지를 바꾼다(시퀀스 올리기 포함)
        if store.unfinished():
            raise HTTPException(409, {"code": "execution_in_progress"})
        try:
            observed = readiness.observe(request)
        except NotObservable as e:
            raise HTTPException(e.http_status, {"code": e.code}) from e
        # A cutover that started meanwhile owns the target now: never hand out this observation.
        if store.unfinished():
            raise HTTPException(409, {"code": "execution_in_progress"})
        return observed

    app.include_router(api)
    return app
