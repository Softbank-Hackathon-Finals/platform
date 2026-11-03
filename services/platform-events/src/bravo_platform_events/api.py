"""Read-only adapter. The host supplies real plan-aware authentication/authorization."""

import asyncio
import inspect
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .contract import encode_sse, validate_identifier
from .store import EventError, EventStore

Authorizer = Callable[[Request, str], bool]


def deny_all(_request: Request, _plan_id: str) -> bool:
    return False


async def event_stream(
    store: EventStore,
    request: Request,
    plan_id: str,
    cursor: str | None,
    authorize: Authorizer,
    *,
    follow: bool,
    poll_seconds: float,
):
    yield "retry: 1000\n\n"
    while not await request.is_disconnected():
        if authorize(request, plan_id) is not True:
            return
        events = await asyncio.to_thread(store.read_after, plan_id, cursor)
        for event in events:
            if authorize(request, plan_id) is not True:
                return
            yield encode_sse(event)
            cursor = event["event_id"]
        if events:
            continue
        if not follow:
            return
        # A comment has no id and does not advance the browser's resume cursor.
        yield ": keep-alive\n\n"
        await asyncio.sleep(poll_seconds)


def create_app(
    store: EventStore,
    *,
    authorize: Authorizer = deny_all,
    poll_seconds: float = 1.0,
) -> FastAPI:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if inspect.iscoroutinefunction(authorize):
        raise ValueError("authorize must be a synchronous predicate")
    app = FastAPI(title="T10 local SSE contract adapter", docs_url=None, redoc_url=None)

    @app.get("/v1/plans/{plan_id}/events")
    async def events(plan_id: str, request: Request, follow: bool = True):
        # Check access before plan existence, cursor validation, or any event read.
        if authorize(request, plan_id) is not True:
            raise HTTPException(403, detail={"code": "forbidden"})
        try:
            validate_identifier(plan_id)
            cursor = request.headers.get("last-event-id") or None
            await asyncio.to_thread(store.read_after, plan_id, cursor)
        except EventError as e:
            status = {"plan_not_found": 404, "invalid_cursor": 400, "resync_required": 409}[e.code]
            raise HTTPException(status, detail={"code": e.code}) from e
        except ValueError as e:
            raise HTTPException(400, detail={"code": "invalid_plan_id"}) from e
        return StreamingResponse(
            event_stream(
                store, request, plan_id, cursor, authorize, follow=follow, poll_seconds=poll_seconds
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app
