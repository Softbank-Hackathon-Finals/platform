"""Host-authenticated CUTOVER routes; no registration or gate assertion input."""

import asyncio
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .change import ChangeApprovalService
from .cutover import CutoverService
from .failback import FailbackService
from .plan_api import Principal, PrincipalResolver, create_plan_app, unauthenticated
from .rehearsal import RehearsalService
from .store import EventError


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_kind: Literal["cutover"]
    binding: dict
    expected_version: StrictInt = Field(ge=1)


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_kind: Literal["change"]
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    binding: dict
    expected_version: StrictInt = Field(ge=1)


class FailbackApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_kind: Literal["failback"]
    binding: dict
    expected_version: StrictInt = Field(ge=1)


class RehearsalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    expected_version: StrictInt = Field(ge=1)
    attempt: StrictInt = Field(ge=1, le=3, default=1)


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def create_cutover_app(service: CutoverService, *,
                       resolve_principal: PrincipalResolver = unauthenticated,
                       change_service: ChangeApprovalService | None = None,
                       failback_service: FailbackService | None = None,
                       rehearsal_service: RehearsalService | None = None):
    for other in (change_service, failback_service, rehearsal_service):
        if other is not None and other.store is not service.store:
            raise ValueError("shared_plan_store_required")
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
        if error.code in ("authority_unavailable", "rehearsal_submission_unknown"):
            status = 503
        return JSONResponse({"detail": {"code": error.code}}, status_code=status)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    if rehearsal_service is not None:
        @app.post("/v1/plans/{plan_id}/rehearsals")
        async def start_rehearsal(plan_id: str, body: RehearsalInput, request: Request,
                                  idempotency_key: str = Header(max_length=128)):
            receipt = await asyncio.to_thread(rehearsal_service.start, principal(request), plan_id,
                                              **body.model_dump(), command_key=idempotency_key)
            return JSONResponse(receipt, status_code=202, headers={"Cache-Control": "no-store"})

        @app.post("/v1/plans/{plan_id}/rehearsals/{rehearsal_id}/attempts/{attempt}/reconcile")
        async def reconcile_rehearsal(plan_id: str, rehearsal_id: str, attempt: int, request: Request):
            receipt = await asyncio.to_thread(rehearsal_service.reconcile, principal(request), plan_id,
                                              rehearsal_id=rehearsal_id, attempt=attempt)
            return JSONResponse(receipt, headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/approvals")
    async def approve(plan_id: str, body: ApprovalInput | ChangeInput | FailbackApprovalInput, request: Request,
                      idempotency_key: str = Header(max_length=128)):
        actor = principal(request)
        approval_service = {"change": change_service, "failback": failback_service}.get(body.approval_kind, service)
        if approval_service is None:
            raise HTTPException(422, detail={"code": "unsupported_approval_kind"})
        receipt = await asyncio.to_thread(approval_service.approve, actor, plan_id,
                                         **body.model_dump(exclude={"approval_kind"}), command_key=idempotency_key)
        # Configuration/connection refs and internal authorization revisions stay private.
        fields = ("approval_id", "approval_kind", "binding", "actor_id", "approved_at")
        if isinstance(body, ChangeInput):
            fields += ("run_id",)
        return JSONResponse({k: receipt[k] for k in fields},
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

    def failback():
        if failback_service is None:
            raise HTTPException(503, detail={"code": "failback_unavailable"})
        return failback_service

    @app.post("/v1/plans/{plan_id}/failback")
    async def submit_failback(plan_id: str, body: SubmitInput, request: Request,
                              idempotency_key: str = Header(max_length=128)):
        actor = principal(request)
        receipt = await asyncio.to_thread(failback().submit, actor, plan_id,
                                         approval_id=body.approval_id, command_key=idempotency_key)
        return JSONResponse(receipt, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/failback/{command_id}/reconcile")
    async def reconcile_failback(plan_id: str, command_id: str, request: Request):
        """GET the Executor and move the plan on. Never sends a new Executor command."""
        actor = principal(request)
        receipt = await asyncio.to_thread(failback().reconcile, actor, plan_id, command_id)
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/failback/{command_id}/edge")
    async def retry_failback_edge(plan_id: str, command_id: str, request: Request):
        """Edge-only retry after the DB part is final. No Executor request."""
        actor = principal(request)
        status = await asyncio.to_thread(failback().advance, actor, plan_id, command_id)
        return JSONResponse({"command_id": command_id, "failback": status}, headers={"Cache-Control": "no-store"})

    return app
