"""Plan snapshot and SSE share the same server-owned tenant access policy."""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .api import create_app
from .contract import validate_identifier
from .plans import PlanStore


@dataclass(frozen=True)
class Principal:
    subject_id: str
    tenant_id: str


PrincipalResolver = Callable[[Request], Principal | None]


def unauthenticated(_request: Request) -> None:
    return None


def create_plan_app(
    store: PlanStore,
    *,
    resolve_principal: PrincipalResolver = unauthenticated,
    poll_seconds: float = 1.0,
):
    """Resolver must use verified host authentication; client tenant headers are not identity."""
    if inspect.iscoroutinefunction(resolve_principal):
        raise ValueError("resolve_principal must be synchronous")

    def authorize(request: Request, plan_id: str) -> bool:
        principal = resolve_principal(request)
        if not isinstance(principal, Principal):
            return False
        try:
            validate_identifier(principal.subject_id)
            validate_identifier(principal.tenant_id)
            return store.tenant_for(plan_id) == principal.tenant_id
        except ValueError:
            return False

    app = create_app(store, authorize=authorize, poll_seconds=poll_seconds)

    @app.get("/v1/plans/{plan_id}")
    async def plan_snapshot(plan_id: str, request: Request):
        if authorize(request, plan_id) is not True:
            raise HTTPException(403, detail={"code": "forbidden"})
        snapshot = await asyncio.to_thread(store.snapshot, plan_id)
        # Do not return a snapshot if the host revoked access while the read was pending.
        if authorize(request, plan_id) is not True:
            raise HTTPException(403, detail={"code": "forbidden"})
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    return app
