"""조회 API(콘솔 T21용)와 밀어 넣기 API(온프렘 에이전트 · Falco 웹훅 · T5 통제 결과)."""

import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from bravo_findings.ingest import Ingestor
from bravo_findings.normalize import CollectionError, Normalizer
from bravo_findings.rules import Rules
from bravo_findings.store import Store
from bravo_findings.view import View

log = logging.getLogger("bravo_findings")

PUSH_SOURCES = {"kyverno", "falco", "bravo_controls"}
AGENT_ERRORS = {"SOURCE_UNHEALTHY", "ACCESS_DENIED", "API_ERROR", "TIMEOUT"}


class IngestBatch(BaseModel):
    environment_id: str = Field(min_length=1, max_length=100)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    agent_error: str | None = None


def create_app(
    store: Store | None = None,
    rules: Rules | None = None,
    tokens: dict[str, str] | None = None,
    clock: Callable[[], int] | None = None,
) -> FastAPI:
    rules = rules or Rules()
    store = store or Store(os.environ.get("BRAVO_FINDINGS_DB", ":memory:"))
    # 환경별 밀어 넣기 토큰. 없으면 밀어 넣기를 받지 않는다.
    tokens = (
        tokens if tokens is not None else json.loads(os.environ.get("BRAVO_INGEST_TOKENS", "{}"))
    )
    clock = clock or (lambda: int(time.time() * 1000))
    ingestor = Ingestor(store, Normalizer(rules))
    view = View(store, rules)

    for item in json.loads(os.environ.get("BRAVO_FINDINGS_SOURCES", "[]")):
        store.register_source(item["source"], item["environment_id"], item.get("enabled", True))

    app = FastAPI(title="bravo-findings")
    app.state.store, app.state.ingestor = store, ingestor

    def authorize(environment_id: str, authorization: str | None, request: Request) -> None:
        expected = tokens.get(environment_id)
        given = (authorization or "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            # 미인증 요청은 화면 상태를 바꾸지 않는다. 기록만 남기고, 진짜 수신이 끊기면 STALE로 드러난다.
            log.warning("unauthorized ingest for %s from %s", environment_id, request.client)
            raise HTTPException(401)

    def accept(
        source: str, environment_id: str, events: list[dict], agent_error: str | None
    ) -> dict:
        now = clock()
        if store.source(source, environment_id) is None:
            store.register_source(source, environment_id)
        if agent_error:
            code = agent_error if agent_error in AGENT_ERRORS else "API_ERROR"
            store.record_failure(source, environment_id, now, code)
            return {"recorded": "failure", "error_code": code}
        try:
            counts = ingestor.ingest(source, environment_id, events, now)
        except CollectionError:
            store.record_failure(source, environment_id, now, "SOURCE_UNHEALTHY")
            return {"recorded": "failure", "error_code": "SOURCE_UNHEALTHY"}
        store.record_success(source, environment_id, now)  # 이벤트가 0건이어도 heartbeat다
        return {"recorded": "success", **counts}

    @app.post("/v1/security/ingest/{source}")
    def ingest(
        source: str,
        batch: IngestBatch,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ):
        if source not in PUSH_SOURCES:
            raise HTTPException(404)
        authorize(batch.environment_id, authorization, request)
        return accept(source, batch.environment_id, batch.events, batch.agent_error)

    @app.post("/v1/security/webhooks/falco/{environment_id}")
    def falco_webhook(
        environment_id: str,
        event: Annotated[dict[str, Any], Body()],
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ):
        """falcosidekick webhook 출력 (custom header로 Authorization: Bearer <토큰>)."""
        authorize(environment_id, authorization, request)
        return accept("falco", environment_id, [event], None)

    @app.get("/v1/security/summary")
    def summary():
        return view.summary(clock())

    @app.get("/v1/security/coverage")
    def coverage():
        return view.capabilities(clock())

    @app.get("/v1/security/sources")
    def sources():
        return view.statuses(clock())

    @app.get("/v1/security/findings")
    def findings(
        environment_id: str | None = None,
        category: Literal[
            "IAM", "ENCRYPTION", "AUDIT", "EXPOSURE", "POLICY", "THREAT", "VULNERABILITY"
        ]
        | None = None,
        min_severity: Annotated[int, Query(ge=0, le=6)] = 0,
        status: Annotated[str, Query(pattern="^(open|all)$")] = "open",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return store.findings(
            environment_id=environment_id,
            category=category,
            min_severity=min_severity,
            open_only=status == "open",
            limit=limit,
            offset=offset,
        )

    @app.post("/v1/security/maintenance/resolve-silent")
    def resolve_silent():
        """Falco처럼 상태가 없는 이벤트를 정해진 시간 뒤 해결 처리한다. 주기 작업이 부른다."""
        hours = rules.mappings["falco_resolve"]["silent_hours"]
        return {"resolved": store.resolve_silent("falco", clock() - hours * 3600 * 1000)}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
