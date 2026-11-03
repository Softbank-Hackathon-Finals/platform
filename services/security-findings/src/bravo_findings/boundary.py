"""Authenticate before body parsing; bound input; never echo rejected payloads."""

import asyncio
import json
import sqlite3
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from bravo_findings.auth import AuthUnavailable

MAX_BODY = 1_048_576
BODY_TIMEOUT_SECONDS = 10


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_json(body):
    def invalid_constant(_):
        raise ValueError("non-finite JSON number")

    value = json.loads(body, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 50_000:
            raise ValueError("JSON complexity limit")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


class SecurityBoundary:
    def __init__(self, app, authenticator, store, clock):
        self.app, self.authenticator, self.store, self.clock = app, authenticator, store, clock

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        principal = None

        async def reject(status, code):
            # No headers, body, query parameters or supplied paths in the audit row.
            self.store.audit(
                self.clock(),
                request_id,
                principal.subject if principal else None,
                "request",
                "denied",
                code,
            )
            response = JSONResponse(
                {"error": code, "request_id": request_id},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)

        headers = Headers(scope=scope)
        if len(headers.getlist("authorization")) != 1:
            return await reject(401, "UNAUTHORIZED")
        try:
            principal = self.authenticator.authenticate(headers.get("authorization"), self.clock())
        except AuthUnavailable:
            return await reject(503, "AUTH_UNAVAILABLE")
        if principal is None:
            return await reject(401, "UNAUTHORIZED")
        scope.setdefault("state", {}).update(principal=principal, request_id=request_id)

        body = bytearray()
        deadline = asyncio.get_running_loop().time() + BODY_TIMEOUT_SECONDS
        while True:
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                chunk = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                return await reject(408, "BODY_TIMEOUT")
            if chunk["type"] == "http.disconnect":
                return
            body.extend(chunk.get("body", b""))
            if len(body) > MAX_BODY:
                return await reject(413, "BODY_TOO_LARGE")
            if not chunk.get("more_body", False):
                break
        if body:
            if headers.get("content-encoding", "identity") != "identity":
                return await reject(415, "UNSUPPORTED_ENCODING")
            if headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return await reject(415, "JSON_REQUIRED")
            try:
                _bounded_json(body)
            except (ValueError, UnicodeError, RecursionError):
                return await reject(400, "INVALID_JSON")

        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def protected_send(message):
            if message["type"] == "http.response.start":
                if message["status"] >= 400:
                    self.store.audit(
                        self.clock(),
                        request_id,
                        principal.subject,
                        "request",
                        "denied",
                        "HTTP_DENIED",
                    )
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        try:
            await self.app(scope, replay, protected_send)
        except sqlite3.Error:
            # A failed durable write is never acknowledged as successful.
            response = JSONResponse(
                {"error": "STORAGE_UNAVAILABLE", "request_id": request_id},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
