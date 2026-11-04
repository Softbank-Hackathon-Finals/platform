"""Dedicated producer HTTP host for the T10 transactional window port.

T36 owns the pinned parser, limits, schema, statistics and intake rejection codes.
This host owns credential resolution and calls WindowIntake.receive once. It does
not expose console, binding-administration, plan creation or execution routes.
"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path

from bravo_observability.intake import IntakeRejected
from bravo_observability.intake_api import (
    MAX_BODY_SECONDS,
    MAX_WINDOW_BYTES,
    STATUS,
    _bounded_structure,
    _constant,
    _finite,
    _object,
)
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .store import EventError
from .window_store import ProducerIdentity, WindowIntake


class FileProducerCredential:
    """Reload the dedicated Secret's hash and server-owned identity per check.

    The plaintext bearer is never a constructor argument, environment variable,
    identity field, database value or log output in this receiver. Mount this
    file from the operator-managed Secret, never from an HTTP upload.
    """

    def __init__(self, path: Path):
        self.path = path

    def __call__(self, request: Request) -> ProducerIdentity | None:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return None
        scheme, separator, token = values[0].partition(" ")
        if (scheme.lower() != "bearer" or not separator or not 32 <= len(token) <= 512
                or not token.isascii() or any(c.isspace() for c in token)):
            return None
        with self.path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("invalid_producer_configuration")
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        if not isinstance(record, dict) or set(record) != {"token_sha256", "identity"}:
            raise ValueError("invalid_producer_configuration")
        digest = record["token_sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid_producer_configuration")
        if not hmac.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), digest):
            return None
        return ProducerIdentity(**record["identity"])


def create_window_app(
    intake: WindowIntake,
    *,
    resolve_producer: Callable[[Request], ProducerIdentity | None] = lambda _request: None,
) -> FastAPI:
    app = FastAPI(title="T10 producer window intake", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def resolve(request):
        try:
            identity = resolve_producer(request)
        except Exception:
            raise HTTPException(503, "authority_unavailable") from None
        if not isinstance(identity, ProducerIdentity):
            raise HTTPException(401, "unauthorized")
        return identity

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            with intake.store._transaction() as connection:
                connection.execute("SELECT 1 FROM sli_windows LIMIT 1").fetchone()
        except Exception:
            raise HTTPException(503, "window_store_unavailable") from None
        return {"status": "ok"}

    @app.post("/v1/observability/windows", status_code=201)
    async def receive_window(request: Request):
        initial = resolve(request)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "encoded_window_not_supported")
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
            document = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                                  parse_constant=_constant, parse_float=_finite)
            _bounded_structure(document)
        except (ValueError, RecursionError):
            raise HTTPException(400, "invalid_json") from None

        def persist():
            current = resolve(request)  # Reload after body upload and thread-pool wait.
            if current != initial:
                raise HTTPException(401, "unauthorized")
            return intake.receive(current, document, revalidate=lambda: resolve(request))

        try:
            stored = await run_in_threadpool(persist)
        except IntakeRejected as error:
            code = error.code if error.code in STATUS else "window_store_unavailable"
            raise HTTPException(STATUS.get(code, 503), code) from None
        except EventError:
            raise HTTPException(403, "forbidden") from None
        except HTTPException:
            raise
        except Exception:
            # No retry: commit acknowledgement loss is not proof of rollback.
            raise HTTPException(503, "window_store_unavailable") from None
        return {"plan_id": stored["plan_id"], "window_start": stored["window"]["start"],
                "received_at": stored["received_at"]}

    return app
