"""Plan-scoped, read-only T37 view. Never a T36 judgement or change authority."""

import asyncio
import math
import re
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .wire import object_document

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
ENV = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ObservationError(ValueError):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


class T37Client:
    """Host-configured fixed origin/token; no browser-supplied URL or PromQL."""

    def __init__(self, origin: str, token: str, *, tls: ssl.SSLContext | None = None):
        if (
            type(origin) is not str
            or any(ord(c) < 33 or ord(c) == 127 for c in origin)
            or "\\" in origin
        ):
            raise ValueError("fixed_secure_observation_origin_required")
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("https", "http")
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or (parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"))
        ):
            raise ValueError("fixed_secure_observation_origin_required")
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or not token.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("invalid_observation_credential")
        if tls is not None and (
            not isinstance(tls, ssl.SSLContext)
            or not tls.check_hostname
            or tls.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ValueError("verified_tls_required")
        self.client = httpx.AsyncClient(
            base_url=origin,
            verify=tls or True,
            timeout=3,
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
        )

    async def http(self, environment):
        if not ENV.fullmatch(environment):
            raise ObservationError("invalid_observation_environment")
        try:
            async with (
                asyncio.timeout(5),
                self.client.stream(
                    "GET", f"/v1/observability/environments/{environment}/http"
                ) as r,
            ):
                r.raise_for_status()
                if (
                    r.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                    or r.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ObservationError("invalid_observation_response")
                raw = bytearray()
                async for part in r.aiter_bytes():
                    if len(raw) + len(part) > 65536:
                        raise ObservationError("observation_response_limit")
                    raw.extend(part)
                return object_document(raw, max_bytes=65536)
        except (httpx.HTTPError, ValueError, RecursionError, TimeoutError):
            raise ObservationError("observation_unavailable") from None

    async def aclose(self):
        await self.client.aclose()


async def _plan(api, scope, plan_id):
    """Use the actual T10 auth/read route before and after an external read."""
    child = dict(
        scope,
        path=f"/v1/plans/{plan_id}",
        raw_path=f"/v1/plans/{plan_id}".encode(),
        query_string=b"",
        method="GET",
    )
    child.pop("endpoint", None)
    child.pop("path_params", None)
    status, raw = None, bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        if message["type"] == "http.response.body":
            part = message.get("body", b"")
            if len(raw) + len(part) > 65536:
                raise ObservationError("plan_response_limit")
            raw.extend(part)

    try:
        await asyncio.wait_for(api(child, receive, send), timeout=3)
        if status in (401, 403, 404):
            raise ObservationError("forbidden", 403)
        if status != 200:
            raise ObservationError("plan_unavailable")
        value = object_document(raw, max_bytes=65536)
        if type(value) is not dict or value.get("plan_id") != plan_id:
            raise ObservationError("plan_mismatch")
        return value
    except ObservationError:
        raise
    except Exception:
        raise ObservationError("plan_unavailable") from None


class PlanObservations:
    def __init__(
        self,
        api,
        client: T37Client,
        *,
        environments: dict[str, str],
        authorize: Callable[[dict, dict], bool],
        clock=None,
    ):
        if (
            not environments
            or not all(ID.fullmatch(k) and ENV.fullmatch(v) for k, v in environments.items())
            or len(set(environments.values())) != len(environments)
        ):
            raise ValueError("registered_observation_mapping_required")
        self.api, self.client, self.environments = api, client, dict(environments)
        self.authorize = authorize
        self.clock = clock or (lambda: datetime.now(UTC))

    def _permit(self, scope, plan):
        # Plan read is necessary, not sufficient for environment-wide metrics.
        # This extra grant must be backed by host permission policy.
        if self.authorize(scope, plan) is not True:
            raise ObservationError("forbidden", 403)

    def _row(self, body, environment, *, now):
        empty = dict(
            environment=environment,
            state="UNOBSERVABLE",
            queried_at=None,
            requests_per_second=None,
            error_ratio=None,
            p95_seconds=None,
        )
        try:
            if (
                type(body) is not dict
                or body.get("environment") != environment
                or body.get("window") != "1m"
            ):
                return empty
            at = datetime.fromisoformat(body["queried_at"].replace("Z", "+00:00"))
            age = body.get("last_received_seconds_ago")
            if (
                at.utcoffset() is None
                or not 0 <= (now - at).total_seconds() <= 10
                or body.get("state") != "OK"
                or type(age) not in (int, float)
                or not math.isfinite(age)
                or not 0 <= age <= 30
            ):
                return empty
            values = {k: body.get(k) for k in ("requests_per_second", "error_ratio", "p95_seconds")}
            for key, value in values.items():
                if value is not None and (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                    or (key == "error_ratio" and value > 1)
                ):
                    return empty
            if values["requests_per_second"] is None:
                return empty
            if values["requests_per_second"] == 0:
                values["error_ratio"] = values["p95_seconds"] = None
            return empty | values | {"state": "OK", "queried_at": at.isoformat()}
        except (KeyError, TypeError, ValueError, OverflowError):
            return empty

    async def read(self, scope, plan_id):
        plan = await _plan(self.api, scope, plan_id)
        self._permit(scope, plan)
        refs = [plan.get(side + "_environment_id") for side in ("source", "target")]
        if any(ref not in self.environments for ref in refs):
            raise ObservationError("observations_unconfigured")
        names = [self.environments[ref] for ref in refs]

        async def fetch(env):
            try:
                return await asyncio.wait_for(self.client.http(env), timeout=5)
            except Exception:
                return None

        bodies = await asyncio.gather(*(fetch(env) for env in names))
        current = await _plan(self.api, scope, plan_id)
        self._permit(scope, current)
        if any(
            current.get(k) != plan.get(k)
            for k in (
                "plan_digest",
                "source_environment_id",
                "target_environment_id",
                "workload_id",
            )
        ):
            raise ObservationError("observation_scope_changed")
        now = self.clock()
        return {
            "kind": "console-observation.v1",
            "plan_id": plan_id,
            "plan_digest": current["plan_digest"],
            "queried_at": now.isoformat(),
            "for_gate_decisions": False,
            "environments": [
                dict(self._row(body, env, now=now), environment_id=ref, side=side)
                for body, env, ref, side in zip(
                    bodies, names, refs, ("source", "target"), strict=True
                )
            ],
        }
