"""Authenticate an existing Actions caller, then read its current T10 input."""

import asyncio
import json
import re
from dataclasses import dataclass

from bravo_agent.contracts import digest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .executor import _object
from .product_inputs import ProductInputRecords
from .store import EventError
from .workload_identity import WorkloadSender

PATH = "/internal/v1/product-inputs/read"
ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
SUBJECT = "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/develop"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class GitHubProductCaller:
    """Pinned public key + existing exact trust; no key URL or token acquisition.

    sts_provider is the Provider string observed in the existing STS response,
    supplied by reviewed deployment config. Do not infer it from the JWT issuer.
    The workload binding's tenant is registered host config, never a JWT claim.
    """

    sender: WorkloadSender
    sts_provider: str

    def __post_init__(self):
        if (type(self.sender) is not WorkloadSender
                or (self.sender.issuer, self.sender.audience, self.sender.subject) != (ISSUER, AUDIENCE, SUBJECT)
                or not isinstance(self.sts_provider, str)
                or re.fullmatch(r"[A-Za-z0-9:/._-]{1,512}", self.sts_provider) is None):
            raise ValueError("reviewed_product_identity_required")

    @property
    def principal(self):
        return digest(dict(Provider=self.sts_provider, Audience=AUDIENCE, SubjectFromWebIdentityToken=SUBJECT))

    def authenticate(self, token, principal):
        claims = self.sender.authenticate(token)
        if (claims.get("repository_id") != "1443045223"
                or claims.get("repository_owner_id") != "350259663"
                or principal != self.principal):
            raise EventError("forbidden")


@dataclass(frozen=True)
class AuthenticatedProductReader:
    records: ProductInputRecords
    caller: GitHubProductCaller

    def __post_init__(self):
        if type(self.records) is not ProductInputRecords or type(self.caller) is not GitHubProductCaller:
            raise ValueError("product_reader_required")

    def read(self, token, principal, input_ref):
        self.caller.authenticate(token, principal)
        record = self.records.read(principal, input_ref)
        # Recheck after waiting on the DB lock, and constrain this host's tenant.
        self.caller.authenticate(token, principal)
        if record["admission"]["tenant_id"] != self.caller.sender.tenant_id:
            raise EventError("forbidden")
        return record


def product_router(reader: AuthenticatedProductReader):
    if type(reader) is not AuthenticatedProductReader:
        raise ValueError("product_reader_required")
    router = APIRouter()

    @router.post(PATH)
    async def read(request: Request):
        def reject(code, status):
            return JSONResponse({"detail": {"code": code}}, status_code=status,
                                headers={"Cache-Control": "no-store"})

        if request.url.scheme != "https":
            return reject("tls_required", 403)
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            return reject("forbidden", 403)
        token = headers[0][7:]
        try:
            reader.caller.authenticate(token, reader.caller.principal)
        except Exception:
            return reject("forbidden", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return reject("invalid_request", 422)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 16384:
                return reject("invalid_request", 413)
        try:
            body = json.loads(raw, object_pairs_hook=_object)
            if type(body) is not dict or set(body) != {"principal", "input_ref"}:
                raise ValueError()
            if (type(body["principal"]) is not str or re.fullmatch(r"[a-f0-9]{64}", body["principal"]) is None
                    or type(body["input_ref"]) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", body["input_ref"]) is None):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            return reject("invalid_request", 422)
        try:
            record = await asyncio.to_thread(reader.read, token, **body)
            response = JSONResponse(record, headers={"Cache-Control": "no-store"})
            if len(response.body) > MAX_RESPONSE_BYTES:
                raise ValueError()
            return response
        except EventError as error:
            if error.code == "forbidden":
                return reject("forbidden", 403)
            return reject("product_input_unavailable", 409)
        except Exception:
            return reject("product_read_unavailable", 503)

    return router


def create_product_read_app(reader):
    """No human IdP, admission, grant management, model or dispatch routes."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(product_router(reader))
    return app
