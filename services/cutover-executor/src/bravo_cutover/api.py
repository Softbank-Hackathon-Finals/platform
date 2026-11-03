"""상태 머신(T10)이 부르는 HTTP API (spec D1: 202 + 상태 조회).

- POST /v1/cutovers: Idempotency-Key 필수. 새 실행이면 202, 같은 키 재요청이면 저장된 실행을 200으로 돌려준다.
- GET /v1/cutovers/{execution_id}, GET /v1/cutovers?plan_id=&epoch=: 응답을 잃었을 때 같은 호출을
  반복하지 않고 상태를 다시 읽는 경로.
"""

import os
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from bravo_cutover.contract import CutoverRequest
from bravo_cutover.store import Rejected, Store


class Runner(Protocol):
    def submit(self, execution_id: UUID) -> None: ...


class NoRunner:
    """실행기 없이 기록만 남긴다(EXECUTOR_CONFIG가 없을 때)."""

    def submit(self, execution_id: UUID) -> None:
        return None


def create_app(store: Store | None = None, runner: Runner | None = None) -> FastAPI:
    """EXECUTOR_DB(실행 기록 DB), EXECUTOR_CONFIG(환경 설정 파일)로 실제 실행기를 만든다."""
    if store is None:
        store = Store(os.environ["EXECUTOR_DB"])
        store.migrate()
    if runner is None and os.environ.get("EXECUTOR_CONFIG"):
        from bravo_cutover.runner import Cutover, ThreadRunner
        from bravo_cutover.sites import load_config

        runner = ThreadRunner(Cutover(store, load_config(os.environ["EXECUTOR_CONFIG"])), store)
    runner = runner or NoRunner()
    app = FastAPI(title="cutover-executor")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/cutovers")
    def start_cutover(
        request: CutoverRequest,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        if not idempotency_key:
            raise HTTPException(400, {"code": "idempotency_key_required"})
        try:
            execution, created = store.create(idempotency_key, request)
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
