"""Original T14 intake on the existing host, with explicit current record grants."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bravo_gate.contract import GateError, decode
from bravo_gate.models import RunSnapshot, identity
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .contract import validate_identifier
from .executor import _timestamp
from .gate_records import GateRecords
from .plan_api import Principal, authorized_plan
from .postgres import StorageUnavailable
from .registration_sso import SsoRegistrationVerifier, make_registration_token
from .store import EventError

AUDIENCE = "bravo-platform-gate-records"
ACTIONS = {"gate:record", "gate:read"}


@dataclass(frozen=True)
class GateRecordPrincipal(Principal):
    issued_at: int
    expires_at: int
    session_id: str


def gate_subject(subject):
    return "gate-record-" + hashlib.sha256(subject.encode()).hexdigest()


def make_gate_record_token(*, profile_name, region):
    return make_registration_token(profile_name=profile_name, region=region, audience=AUDIENCE)


class GateRecordVerifier:
    def __init__(self, verifier):
        if type(verifier) is not SsoRegistrationVerifier or verifier.audience != AUDIENCE:
            raise ValueError("gate_record_identity_required")
        self.verifier = verifier

    def __call__(self, request):
        actor = self.verifier(request)
        if actor is None:
            return None
        return GateRecordPrincipal(
            gate_subject(actor.subject_id),
            actor.tenant_id,
            actor.issued_at,
            actor.expires_at,
            actor.session_id,
        )


class GateRecordGrants:
    """Protected mount, read under each plan transaction; no enrollment endpoint.

    Each grant pins one original side/ref/digest and the registered plan digest.
    It authorizes staging/reading only, never T19 provenance, evaluation or S2/S3.
    """

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("gate_record_grant_mount_required")
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())

    def require(self, actor, plan, action, ref, *, digest=None, side=None):
        try:
            now = self.clock()
            if (
                type(actor) is not GateRecordPrincipal
                or action not in ACTIONS
                or actor.tenant_id != plan["tenant_id"]
                or not actor.issued_at <= now < actor.expires_at
            ):
                raise ValueError()
            with self.path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            data = decode(raw)
            if (
                set(data) != {"version", "grants"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["grants"]) is not list
                or len(data["grants"]) > 64
            ):
                raise ValueError()
            selected, seen = [], set()
            for grant in data["grants"]:
                if type(grant) is not dict or set(grant) != {
                    "subject_id",
                    "tenant_id",
                    "plan_id",
                    "plan_digest",
                    "ref",
                    "digest",
                    "side",
                    "actions",
                    "valid_after",
                    "expires_at",
                    "administration_ref",
                }:
                    raise ValueError()
                for name in ("subject_id", "tenant_id", "plan_id", "administration_ref"):
                    validate_identifier(grant[name])
                identity(grant["ref"])
                for name in ("plan_digest", "digest"):
                    if (
                        type(grant[name]) is not str
                        or len(grant[name]) != 64
                        or any(c not in "0123456789abcdef" for c in grant[name])
                    ):
                        raise ValueError()
                if (
                    grant["side"] not in {"source", "target"}
                    or type(grant["actions"]) is not list
                    or not grant["actions"]
                    or len(set(grant["actions"])) != len(grant["actions"])
                    or not set(grant["actions"]).issubset(ACTIONS)
                ):
                    raise ValueError()
                key = tuple(grant[k] for k in ("subject_id", "tenant_id", "plan_id", "ref"))
                if key in seen:
                    raise ValueError()
                seen.add(key)
                start, end = (
                    _timestamp(grant[k]).timestamp() for k in ("valid_after", "expires_at")
                )
                if not start < end:
                    raise ValueError()
                if (
                    key == (actor.subject_id, actor.tenant_id, plan["plan_id"], ref)
                    and grant["plan_digest"] == json.loads(plan["spec_json"])["plan_digest"]
                    and action in grant["actions"]
                    and start <= now < end
                    and (digest is None or digest == grant["digest"])
                    and (side is None or side == grant["side"])
                ):
                    selected.append(grant)
            if len(selected) != 1:
                raise ValueError()
            return selected[0]
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise EventError("forbidden") from None


class GateRecordHost:
    def __init__(self, store, grants):
        if type(grants) is not GateRecordGrants:
            raise ValueError("gate_record_grants_required")
        self.store, self.grants = store, grants

    def record(self, actor, plan_id, side, snapshot):
        def authorize(_c, principal, plan, action):
            self.grants.require(
                principal, plan, action, snapshot.ref, digest=snapshot.fingerprint, side=side
            )
            return True

        return GateRecords(self.store, authorize=authorize).record_run(
            actor, plan_id, side, snapshot
        )

    def read(self, actor, plan_id, ref):
        def authorize(_c, principal, plan, action):
            self.grants.require(principal, plan, action, ref)
            return True

        records = GateRecords(self.store, authorize=authorize)
        with self.store._transaction() as c:
            plan = authorized_plan(c, actor, plan_id, None)
            snapshot = records.read_run(c, actor, plan, ref)
            self.grants.require(
                actor,
                plan,
                "gate:read",
                ref,
                digest=snapshot.fingerprint,
                side=snapshot.document["environment"]["role"],
            )
            return dict(ref=ref, digest=snapshot.fingerprint, document=snapshot.document)


def create_gate_record_app(host, verifier):
    if type(host) is not GateRecordHost or type(verifier) is not GateRecordVerifier:
        raise ValueError("gate_record_host_required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    async def principal(request):
        if request.url.scheme != "https":
            raise HTTPException(403, detail={"code": "tls_required"})
        try:
            actor = await asyncio.to_thread(verifier, request)
        except Exception:
            raise HTTPException(503, detail={"code": "gate_identity_unavailable"}) from None
        if type(actor) is not GateRecordPrincipal:
            raise HTTPException(403, detail={"code": "forbidden"})
        return actor

    @app.exception_handler(EventError)
    async def rejected(_request, error):
        return JSONResponse(
            {"detail": {"code": error.code}},
            status_code=403 if error.code == "forbidden" else 409,
            headers={"Cache-Control": "no-store"},
        )

    async def invalid(_request, _error):
        return JSONResponse(
            {"detail": {"code": "invalid_gate_record"}},
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    for kind in (ValueError, GateError, KeyError, TypeError):
        app.add_exception_handler(kind, invalid)

    async def unavailable(_request, _error):
        return JSONResponse(
            {"detail": {"code": "gate_records_unavailable"}},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    for kind in (sqlite3.Error, StorageUnavailable, RuntimeError):
        app.add_exception_handler(kind, unavailable)

    @app.post("/v1/plans/{plan_id}/runs")
    async def record(plan_id: str, request: Request):
        actor = await principal(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1048576:
                raise HTTPException(413, detail={"code": "gate_record_too_large"})
        value = decode(bytes(raw))
        if set(value) != {"ref", "side", "document"}:
            raise ValueError()
        from bravo_gate.contract import canonical

        snapshot = RunSnapshot(value["ref"], canonical(value["document"]))
        result = await asyncio.to_thread(host.record, actor, plan_id, value["side"], snapshot)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/plans/{plan_id}/runs")
    async def read(plan_id: str, ref: str, request: Request):
        actor = await principal(request)
        result = await asyncio.to_thread(host.read, actor, plan_id, ref)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app
