"""Host-authenticated CUTOVER routes; no registration or gate assertion input."""

import asyncio
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .cutover import CutoverService
from .plan_api import Principal, PrincipalResolver, create_plan_app, unauthenticated
from .store import EventError


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_kind: Literal["cutover"]
    binding: dict
    expected_version: StrictInt = Field(ge=1)


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def create_cutover_app(service: CutoverService, *,
                       resolve_principal: PrincipalResolver = unauthenticated):
    app = create_plan_app(service.store, resolve_principal=resolve_principal)

    def principal(request):
        identity = resolve_principal(request)
        if not isinstance(identity, Principal):
            raise HTTPException(403, detail={"code": "forbidden"})
        return identity

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    @app.exception_handler(EventError)
    async def rejected(_request, error):
        status = 403 if error.code == "forbidden" else 409
        if error.code == "authority_unavailable":
            status = 503
        return JSONResponse({"detail": {"code": error.code}}, status_code=status)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    @app.post("/v1/plans/{plan_id}/approvals")
    async def approve(plan_id: str, body: ApprovalInput, request: Request,
                      idempotency_key: str = Header(max_length=128)):
        receipt = await asyncio.to_thread(service.approve, principal(request), plan_id,
                                         binding=body.binding, expected_version=body.expected_version,
                                         command_key=idempotency_key)
        # Configuration/connection refs and internal authorization revisions stay private.
        return JSONResponse({k: receipt[k] for k in
                             ("approval_id", "approval_kind", "binding", "actor_id", "approved_at")},
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/cutover")
    async def submit(plan_id: str, body: SubmitInput, request: Request,
                     idempotency_key: str = Header(max_length=128)):
        receipt = await asyncio.to_thread(service.submit, principal(request), plan_id,
                                         approval_id=body.approval_id, command_key=idempotency_key)
        return JSONResponse(receipt, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/cutover/{command_id}/reconcile")
    async def reconcile(plan_id: str, command_id: str, request: Request):
        receipt = await asyncio.to_thread(service.reconcile, principal(request), plan_id, command_id)
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})

    return app
