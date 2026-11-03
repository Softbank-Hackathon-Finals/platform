"""상태 머신(T10)이 부르는 HTTP API. 실행 요청 · 상태 조회는 다음 커밋에서 붙인다."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="cutover-executor")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    return app
