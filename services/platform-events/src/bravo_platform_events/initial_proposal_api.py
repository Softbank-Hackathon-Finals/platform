"""Read one current initial PROPOSE input through the separately scoped OIDC route."""

import asyncio
import json
from dataclasses import dataclass

from bravo_agent.contracts import Contract, digest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .cutover import _get
from .executor import _object
from .initial_proposal import InitialInput, InitialIntent, InitialProposalRecords, _slot
from .product_api import MAX_RESPONSE_BYTES, KubernetesJobCaller
from .store import EventError

PATH = "/internal/v1/initial-proposal-inputs/read"


class InitialRead(Contract):
    principal: str = Field(pattern=r"^[a-f0-9]{64}$")
    intent: InitialIntent
    github_run_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


def bound_input(intent, raw):
    value = InitialInput.model_validate_json(json.dumps(raw, allow_nan=False))
    if (value.schema_version != 2 or digest(value) != intent.input_digest
            or digest(value.binding) != intent.binding_digest
            or (value.binding.plan_id, value.binding.plan_version, value.binding.purpose) !=
               (intent.plan_id, intent.plan_version, intent.purpose)):
        raise ValueError("initial_input_conflict")
    return value


@dataclass(frozen=True)
class AuthenticatedInitialReader:
    records: InitialProposalRecords
    caller: KubernetesJobCaller

    def __post_init__(self):
        if (type(self.records) is not InitialProposalRecords
                or type(self.caller) is not KubernetesJobCaller):
            raise ValueError("initial_reader_required")

    def authenticate(self, token, request):
        # The bound Pod name carries the operator's execution ID (replaces Actions run_id claims).
        claims = self.caller.authenticate(token, request.principal)
        if self.caller.execution_id(claims) != request.github_run_id:
            raise EventError("forbidden")

    def read(self, token, request):
        self.authenticate(token, request)
        intent = request.intent
        fields = intent.model_dump(exclude={"request_id", "recipient_public_key"})
        if intent.request_id != "t10p-" + digest(fields | {"principal": request.principal}):
            raise EventError("forbidden")
        # Read the exact committed dispatch claim and current grant in one DB snapshot.
        # Registration alone does not authorize an unclaimed caller to run a model.
        with self.records.store._transaction() as c:
            value = self.records._read(c, request.principal, intent.plan_id, intent.plan_version,
                {"input_ref": intent.input_ref, "input_digest": intent.input_digest})
            claimed = _get(c, intent.plan_id, _slot(intent.plan_version) + ":execution")
            if not claimed or claimed != (intent.model_dump(mode="json"), {"state": "CLAIMED"}):
                raise EventError("initial_execution_unavailable")
            raw = value.model_dump(mode="json")
        self.authenticate(token, request)  # A DB wait must not extend the token's lifetime.
        value = bound_input(intent, raw)
        if value.binding.tenant_id != self.caller.sender.tenant_id:
            raise EventError("forbidden")
        return value.model_dump(mode="json")


def create_initial_read_app(reader):
    if type(reader) is not AuthenticatedInitialReader:
        raise ValueError("initial_reader_required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(PATH)
    async def read(request: Request):
        def reject(code, status):
            return JSONResponse({"detail": {"code": code}}, status_code=status,
                                headers={"Cache-Control": "no-store"})

        headers = request.headers.getlist("authorization")
        if request.url.scheme != "https":
            return reject("tls_required", 403)
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
            body = InitialRead.model_validate(json.loads(raw, object_pairs_hook=_object))
        except (ValueError, TypeError, RecursionError):
            return reject("invalid_request", 422)
        try:
            record = await asyncio.to_thread(reader.read, token, body)
            response = JSONResponse(record, headers={"Cache-Control": "no-store"})
            if len(response.body) > MAX_RESPONSE_BYTES:
                raise ValueError()
            return response
        except EventError as error:
            return reject("forbidden" if error.code == "forbidden" else "initial_input_unavailable",
                          403 if error.code == "forbidden" else 409)
        except Exception:
            return reject("initial_read_unavailable", 503)

    return app
