"""Explicit T15 candidate transport; originals and hold authority stay in the host."""

import asyncio
import json
import math
import re
import ssl
from dataclasses import asdict, fields
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .change_intake import Candidate, ChangeIntake
from .store import EventError

PATH = "/internal/v1/rehearsal-candidates"


def intake_router(intake: ChangeIntake) -> APIRouter:
    router = APIRouter()

    @router.post(PATH)
    async def receive(request: Request):
        def reply(body, status):
            return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

        def reject(code, status):
            return reply({"detail": {"code": code}}, status)

        # Host must terminate TLS, or set the scheme from an authenticated proxy.
        # Do not trust arbitrary X-Forwarded-Proto headers here.
        if request.url.scheme != "https":
            return reject("tls_required", 403)
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
            return reject("forbidden", 403)
        token = authorization[0][7:]
        try:
            intake.sender.authenticate(token)
        except EventError:
            return reject("forbidden", 403)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return reject("invalid_request", 422)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384:
                return reject("invalid_request", 413)
        try:
            body = json.loads(raw)
            if type(body) is not dict or set(body) != {f.name for f in fields(Candidate)}:
                raise ValueError()
            candidate = Candidate(**body)
            candidate.validate()
        except (ValueError, TypeError, RecursionError):
            return reject("invalid_request", 422)
        try:
            # receive authenticates again before original lookup and after the
            # plan lock. HTTP fields never become SourceVerification or a hold.
            receipt = await asyncio.to_thread(intake.receive, token, candidate)
        except EventError as error:
            status = 403 if error.code == "forbidden" else 409
            if error.code == "authority_unavailable":
                status = 503
            return reject(error.code, status)
        except Exception:
            return reject("intake_unavailable", 503)
        return reply(receipt, 200)

    return router


class IntakeDeliveryUnknown(RuntimeError):
    """No validated acknowledgement. Never retry a rehearsal as a consequence."""


class HttpChangeIntake:
    """Implements ChangeIntake.receive for T15 submit_candidate, with one POST.

    The host pins the HTTPS origin and CA. The workload token is supplied for
    each call, never persisted. Redirects, environment proxies and retries are off.
    """

    def __init__(self, origin: str, *, timeout_seconds=5.0, tls_context=None, transport=None):
        try:
            url = urlsplit(origin)
            valid = (url.scheme == "https" and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and url.path in ("", "/") and (url.port is None or url.port > 0))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("invalid_intake_origin")
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 30):
            raise ValueError("invalid_intake_timeout")
        if tls_context is not None and (not isinstance(tls_context, ssl.SSLContext)
                or tls_context.verify_mode != ssl.CERT_REQUIRED or not tls_context.check_hostname):
            raise ValueError("verified_tls_context_required")
        self._client = httpx.Client(base_url=origin.rstrip("/"), timeout=timeout_seconds,
            verify=tls_context or True, transport=transport, trust_env=False, follow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"})

    def close(self):
        self._client.close()

    def receive(self, token: str, candidate: Candidate):
        if (type(candidate) is not Candidate or not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,16384}", token)):
            raise ValueError("invalid_intake_delivery")
        candidate.validate()
        try:
            with self._client.stream("POST", PATH, json=asdict(candidate),
                                     headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status_code != 200:
                    raise ValueError()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        raise ValueError()
                receipt = json.loads(raw)
            if (type(receipt) is not dict or set(receipt) != {"verification_id", "attempt", "revision"}
                    or receipt["verification_id"] != candidate.verification_id
                    or type(receipt["attempt"]) is not int or receipt["attempt"] != candidate.attempt
                    or not isinstance(receipt["revision"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", receipt["revision"])):
                raise ValueError()
        except Exception:
            raise IntakeDeliveryUnknown("intake_delivery_unknown") from None
        return receipt
