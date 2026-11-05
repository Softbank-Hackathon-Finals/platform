"""Explicit SSO start/read authority for one rehearsal; no S2/S3 authority."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .contract import validate_identifier
from .cutover_api import RehearsalInput
from .executor import _object, _timestamp
from .plan_api import Principal, authorized_plan
from .postgres import StorageUnavailable
from .registration_sso import SsoRegistrationVerifier, make_registration_token
from .rehearsal import RehearsalService
from .store import EventError

AUDIENCE = "bravo-platform-rehearsal"


@dataclass(frozen=True)
class SsoRehearsalPrincipal(Principal):
    issued_at: int
    expires_at: int
    session_id: str


def rehearsal_subject(registration_subject):
    return "rehearsal-" + hashlib.sha256(registration_subject.encode()).hexdigest()


def make_rehearsal_token(*, profile_name, region):
    """Keep the proof in memory; its signed audience cannot register or approve."""
    return make_registration_token(profile_name=profile_name, region=region, audience=AUDIENCE)


class SsoRehearsalVerifier:
    """Reuse the reviewed STS proof, with a different signed audience and type."""

    def __init__(self, identity):
        if type(identity) is not SsoRegistrationVerifier or identity.audience != AUDIENCE:
            raise ValueError("rehearsal_sso_identity_required")
        self.identity = identity

    def __call__(self, request):
        actor = self.identity(request)
        if actor is None:
            return None
        return SsoRehearsalPrincipal(rehearsal_subject(actor.subject_id), actor.tenant_id,
                                    actor.issued_at, actor.expires_at, actor.session_id)


class RehearsalGrantFile:
    """Reviewed host mount, read inside every plan lock. Never an HTTP grant API.

    Each entry permits only start/reconcile/snapshot for its exact tenant/plan.
    Removing an entry revokes it. The operator records the source revision and
    administration decision when provisioning the protected mount.
    """

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("rehearsal_grant_mount_required")
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())

    def require(self, _connection, actor, plan):
        now = self.clock()
        if (type(actor) is not SsoRehearsalPrincipal or actor.tenant_id != plan["tenant_id"]
                or not actor.issued_at <= now < actor.expires_at):
            raise EventError("forbidden")
        try:
            with self.path.open() as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            data = json.loads(raw, object_pairs_hook=_object)
            if type(data) is not dict or set(data) != {"version", "grants"} or type(data["version"]) is not int or data["version"] != 1:
                raise ValueError()
            if type(data["grants"]) is not list or len(data["grants"]) > 64:
                raise ValueError()
            matches = []
            seen = set()
            for grant in data["grants"]:
                if type(grant) is not dict or set(grant) != {"subject_id", "tenant_id", "plan_id", "valid_after", "expires_at", "administration_ref"}:
                    raise ValueError()
                for key in ("subject_id", "tenant_id", "plan_id", "administration_ref"):
                    validate_identifier(grant[key])
                identity = tuple(grant[k] for k in ("subject_id", "tenant_id", "plan_id"))
                if identity in seen:
                    raise ValueError()
                seen.add(identity)
                start, end = (_timestamp(grant[k]).timestamp() for k in ("valid_after", "expires_at"))
                if not start < end:
                    raise ValueError()
                if identity == (actor.subject_id, actor.tenant_id, plan["plan_id"]) and start <= now < end:
                    matches.append(grant)
            if len(matches) != 1:
                raise ValueError()
        except (OSError, TypeError, ValueError, KeyError, RecursionError):
            raise EventError("forbidden") from None


def create_rehearsal_app(service, *, verifier, access):
    """First-attempt endpoints only. There are no grant, approval or execute routes."""
    if (type(service) is not RehearsalService or type(verifier) is not SsoRehearsalVerifier
            or type(access) is not RehearsalGrantFile or service.check_start != access.require):
        raise ValueError("rehearsal_host_binding_required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def principal(request):
        if request.url.scheme != "https":
            raise HTTPException(403, detail={"code": "tls_required"})
        try:
            actor = await asyncio.to_thread(verifier, request)
        except Exception:
            raise HTTPException(503, detail={"code": "rehearsal_identity_unavailable"}) from None
        if type(actor) is not SsoRehearsalPrincipal:
            raise HTTPException(403, detail={"code": "forbidden"})
        return actor

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    async def unavailable(_request, _error):
        return JSONResponse({"detail": {"code": "rehearsal_unavailable"}}, status_code=503,
                            headers={"Cache-Control": "no-store"})

    for error_type in (sqlite3.Error, StorageUnavailable, RuntimeError):
        app.add_exception_handler(error_type, unavailable)

    @app.exception_handler(EventError)
    async def rejected(_request, error):
        status = 403 if error.code == "forbidden" else 409
        if error.code in {"authority_unavailable", "rehearsal_submission_unknown", "rehearsal_verification_unavailable"}:
            status = 503
        return JSONResponse({"detail": {"code": error.code}}, status_code=status,
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/rehearsals")
    async def start(plan_id: str, body: RehearsalInput, request: Request,
                    idempotency_key: str = Header(max_length=128)):
        actor = await principal(request)
        if body.attempt != 1 or len(request.headers.getlist("Idempotency-Key")) != 1:
            raise HTTPException(422, detail={"code": "first_attempt_required"})
        result = await asyncio.to_thread(service.start, actor, plan_id, **body.model_dump(), command_key=idempotency_key)
        return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})

    @app.post("/v1/plans/{plan_id}/rehearsals/{rehearsal_id}/attempts/1/reconcile")
    async def reconcile(plan_id: str, rehearsal_id: str, request: Request):
        actor = await principal(request)
        result = await asyncio.to_thread(service.reconcile, actor, plan_id, rehearsal_id=rehearsal_id, attempt=1)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/plans/{plan_id}")
    async def snapshot(plan_id: str, request: Request):
        actor = await principal(request)

        def read():
            with service.store._transaction() as c:
                authorized_plan(c, actor, plan_id, access.require)
            result = service.store.snapshot(plan_id)
            with service.store._transaction() as c:
                authorized_plan(c, actor, plan_id, access.require)
            return result

        return JSONResponse(await asyncio.to_thread(read), headers={"Cache-Control": "no-store"})

    return app
