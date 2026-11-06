"""Registration routes for a trusted operator host. The window ingress stays separate."""

import asyncio
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .registration_probe import ProbeUnavailable
from .store import EventError

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")]
Profile = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Key = Annotated[str, Header(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Environment(Input):
    name: Name
    kind: Literal["aws", "onprem"]
    roles: list[Literal["source", "target"]] = Field(min_length=1, max_length=1)
    connection_ref: Identifier


class Workload(Input):
    name: Name
    repo_url: str = Field(min_length=1, max_length=512)
    app_path: str = Field(min_length=1, max_length=256)
    iac_path: str = Field(min_length=1, max_length=256)
    source_environment_id: Identifier
    namespace: Identifier


class Plan(Input):
    workload_id: Identifier
    target_environment_id: Identifier
    profile_id: Profile
    profile_version: Profile
    recheck_profile_id: Profile | None = None
    recheck_profile_version: Profile | None = None

    @model_validator(mode="after")
    def recheck_pair(self):
        fields = {"recheck_profile_id", "recheck_profile_version"}
        if fields & self.model_fields_set:
            if not fields <= self.model_fields_set or None in (self.recheck_profile_id, self.recheck_profile_version):
                raise ValueError("invalid_recheck_profile")
        return self


def create_registration_app(registry, *, verify_identity):
    """Mount behind the operator TLS ingress with trusted host configuration."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    install_registration_routes(app, registry, verify_identity=verify_identity)
    return app


def install_registration_routes(app, registry, *, verify_identity):
    """Attach to an existing operator API with its safe validation-error handler."""
    async def submit(request, kind, body, key):
        if len(request.headers.getlist("Idempotency-Key")) != 1:
            raise HTTPException(422, detail={"code": "invalid_request"})
        try:
            actor = await asyncio.to_thread(verify_identity, request)
            receipt, created = await asyncio.to_thread(registry.register, actor, kind, body.model_dump(exclude_unset=True), key)
        except EventError as e:
            status = {"forbidden": 403, "registration_reference_unavailable": 404}.get(e.code, 409)
            raise HTTPException(status, detail={"code": e.code}) from None
        except ProbeUnavailable as e:
            code = str(e)
            conflict = code in {"environment_replaced", "repo_mismatch", "revision_mismatch",
                                "release_source_mismatch", "deployment_release_mismatch", "replica_scope_mismatch"}
            raise HTTPException(409 if conflict else 503, detail={"code": code}) from None
        except (sqlite3.Error, RuntimeError):
            raise HTTPException(503, detail={"code": "registration_unavailable"}) from None
        return JSONResponse(receipt, status_code=201 if created else 200, headers={"Cache-Control": "no-store"})

    @app.post("/v1/environments")
    async def environment(request: Request, body: Environment, idempotency_key: Key):
        return await submit(request, "environment", body, idempotency_key)

    @app.post("/v1/workloads")
    async def workload(request: Request, body: Workload, idempotency_key: Key):
        return await submit(request, "workload", body, idempotency_key)

    @app.post("/v1/plans")
    async def plan(request: Request, body: Plan, idempotency_key: Key):
        return await submit(request, "plan", body, idempotency_key)
