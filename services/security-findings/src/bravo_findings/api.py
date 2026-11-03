"""T33 internal API. Every data route requires a scoped service credential."""

import json
import os
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bravo_findings.auth import ID_PATTERN, Authenticator, AuthUnavailable
from bravo_findings.boundary import SecurityBoundary
from bravo_findings.ingest import Ingestor
from bravo_findings.normalize import CollectionError, Normalizer
from bravo_findings.rules import Rules
from bravo_findings.store import Store
from bravo_findings.view import View

PUSH_SOURCES = {"kyverno", "falco", "bravo_controls"}
AGENT_ERRORS = {"SOURCE_UNHEALTHY", "ACCESS_DENIED", "API_ERROR", "TIMEOUT"}


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    environment_id: str = Field(pattern=ID_PATTERN)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    agent_error: str | None = Field(default=None, max_length=40)


def create_app(
    store: Store | None = None,
    rules: Rules | None = None,
    credentials: list[dict] | None = None,
    clock: Callable[[], int] | None = None,
    auth_file: str | None = None,
) -> FastAPI:
    if os.environ.get("BRAVO_INGEST_TOKENS"):
        raise ValueError("legacy ingest tokens are unsupported; configure scoped credentials")
    rules = rules or Rules()
    db_path = os.environ.get("BRAVO_FINDINGS_DB")
    if store is None and (not db_path or db_path == ":memory:"):
        raise ValueError("BRAVO_FINDINGS_DB is required; production state must persist")
    store = store or Store(db_path)
    clock = clock or (lambda: int(time.time() * 1000))
    authenticator = Authenticator(
        credentials, auth_file or os.environ.get("BRAVO_FINDINGS_AUTH_FILE")
    )
    ingestor = Ingestor(store, Normalizer(rules))
    view = View(store, rules)
    for item in json.loads(os.environ.get("BRAVO_FINDINGS_SOURCES", "[]")):
        store.register_source(item["source"], item["environment_id"], item.get("enabled", True))
    app = FastAPI(title="bravo-findings", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.ingestor = store, ingestor
    app.add_middleware(SecurityBoundary, authenticator=authenticator, store=store, clock=clock)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic errors include input values; never return or log them.
        store.audit(
            clock(),
            request.state.request_id,
            request.state.principal.subject,
            "request",
            "denied",
            "INVALID_REQUEST",
        )
        return JSONResponse({"error": "INVALID_REQUEST"}, status_code=422)

    def authorize(request, permission, environment_id=None, source=None):
        # Refresh revocation/expiry after waiting for the SQLite transaction lock.
        try:
            actor = authenticator.authenticate(request.headers.get("authorization"), clock())
        except AuthUnavailable:
            raise HTTPException(503, "AUTH_UNAVAILABLE") from None
        if actor is None:
            raise HTTPException(401, "UNAUTHORIZED")
        if (
            permission not in actor.permissions
            or (environment_id is not None and environment_id not in actor.environments)
            or (source is not None and source not in actor.sources)
        ):
            store.audit(
                clock(),
                request.state.request_id,
                actor.subject,
                permission,
                "denied",
                "SCOPE_DENIED",
            )
            raise HTTPException(403, "FORBIDDEN")
        return actor

    def accept(request, source, environment_id, events, agent_error=None):
        authorize(request, "ingest", environment_id, source)
        with store.batch():
            actor = authorize(request, "ingest", environment_id, source)
            now = clock()
            row = store.source(source, environment_id)
            if row is not None and not row.enabled:
                raise HTTPException(409, "SOURCE_DISABLED")
            if row is None:
                store.register_source(source, environment_id)
            if agent_error:
                code = agent_error if agent_error in AGENT_ERRORS else "API_ERROR"
                store.record_failure(source, environment_id, now, code)
                result = {"recorded": "failure", "error_code": code}
            else:
                try:
                    counts = ingestor.ingest(source, environment_id, events, now)
                    if counts["source_errors"]:
                        store.record_failure(source, environment_id, now, "SOURCE_UNHEALTHY")
                    else:
                        store.record_success(source, environment_id, now)
                    code = store.source(source, environment_id).error_code
                    result = {"recorded": "failure" if code else "success", **counts}
                    if code:
                        result["error_code"] = code
                except CollectionError:
                    store.record_failure(source, environment_id, now, "SOURCE_UNHEALTHY")
                    result = {"recorded": "failure", "error_code": "SOURCE_UNHEALTHY"}
            store.audit(
                now,
                request.state.request_id,
                actor.subject,
                "ingest",
                result["recorded"],
                result.get("error_code", "ACCEPTED"),
                environment_id,
                source,
            )
            return result

    @app.post("/v1/security/ingest/{source}")
    def ingest(source: str, batch: IngestBatch, request: Request):
        if source not in PUSH_SOURCES:
            raise HTTPException(404)
        return accept(request, source, batch.environment_id, batch.events, batch.agent_error)

    @app.post("/v1/security/webhooks/falco/{environment_id}")
    def falco_webhook(
        environment_id: str, event: Annotated[dict[str, Any], Body()], request: Request
    ):
        return accept(request, "falco", environment_id, [event])

    def read_scope(request, environment_id=None):
        actor = authorize(request, "read", environment_id)
        store.audit(
            clock(),
            request.state.request_id,
            actor.subject,
            "read",
            "allowed",
            "AUTHORIZED",
            environment_id,
        )
        return {environment_id} if environment_id else set(actor.environments)

    @app.get("/v1/security/summary")
    def summary(request: Request, environment_id: str | None = None):
        return view.summary(clock(), read_scope(request, environment_id))

    @app.get("/v1/security/coverage")
    def coverage(request: Request, environment_id: str | None = None):
        return view.capabilities(clock(), read_scope(request, environment_id))

    @app.get("/v1/security/sources")
    def sources(request: Request, environment_id: str | None = None):
        return view.statuses(clock(), read_scope(request, environment_id))

    @app.get("/v1/security/findings")
    def findings(
        request: Request,
        environment_id: str | None = None,
        category: Literal[
            "IAM", "ENCRYPTION", "AUDIT", "EXPOSURE", "POLICY", "THREAT", "VULNERABILITY"
        ]
        | None = None,
        min_severity: Annotated[int, Query(ge=0, le=6)] = 0,
        status: Annotated[str, Query(pattern="^(open|all)$")] = "open",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ):
        allowed = read_scope(request, environment_id)
        return store.findings(
            environments=allowed,
            category=category,
            min_severity=min_severity,
            open_only=status == "open",
            limit=limit,
            offset=offset,
        )

    @app.post("/v1/security/maintenance/resolve-silent")
    def resolve_silent(request: Request):
        authorize(request, "read")
        store.audit(
            clock(),
            request.state.request_id,
            request.state.principal.subject,
            "resolve_silent",
            "denied",
            "SILENCE_IS_NOT_RESOLUTION",
        )
        raise HTTPException(410, "SILENCE_IS_NOT_RESOLUTION")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
