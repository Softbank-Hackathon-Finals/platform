"""Read-only shared network atlas. Never an app graph or cutover authority.

The operator registers the atlas origin and environment pair on the server.
An explicit network-wide grant is required in addition to the native plan read.
No cluster credential, collector, DNS operation or browser URL is accepted.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit

import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect

from .observations import ObservationError, _plan
from .wire import object_document
from .workspace_data import call

SOURCE_IDS = {
    "aws_network",
    "platform_k8s",
    "target_k8s",
    "source_k8s",
    "tailscale_human",
    "tailscale_workload",
    "local_underlay",
    "platform_api",
    "target_host",
}
ROW_KEYS = {
    "resources": ("kind", "namespace", "name", "status", "ip", "ports", "ready", "detail"),
    "networks": ("layer", "name", "cidr", "next_hop", "nat", "detail"),
    "links": ("from", "to", "transport", "state", "detail"),
}
ERRORS = {
    "TIMEOUT",
    "AUTH_EXPIRED",
    "FORBIDDEN",
    "CONNECTION_FAILED",
    "NOT_CONFIGURED",
    "PARTIAL_READ",
    "PROVIDER_ERROR",
    "IDENTITY_MISMATCH",
}
STAGES = {
    "preflight",
    "input",
    "proposal",
    "rehearsal",
    "approval",
    "deploy",
    "verify",
    "cutover",
    "cleanup",
}
STATUSES = {"waiting", "running", "blocked", "pass", "fail", "done", "unknown"}
REASONS = {
    "NONE",
    "WAITING_INPUT",
    "WAITING_REVIEW",
    "WAITING_CI",
    "WAITING_WINDOW",
    "AUTH_DENIED",
    "CONNECTION_FAILED",
    "VALIDATION_FAILED",
    "DEPENDENCY_BLOCKED",
    "EXECUTION_FAILED",
    "COMPLETE",
    "UNKNOWN",
}


def clean_text(value, limit=1200):
    if type(value) not in (str, int, float, bool):
        raise ValueError("atlas_field_type")
    value = str(value)
    if len(value) > limit or re.search(
        r"-----BEGIN|Bearer\s+|(?:AKIA|ASIA)[A-Z0-9]{16}|tskey-|(?:password|token|secret)\s*=",
        value,
        re.I,
    ):
        raise ValueError("unsafe_atlas_field")
    return value


def timestamp(value, now, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str:
        raise ValueError("atlas_time_type")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or (parsed - now).total_seconds() > 60:
        raise ValueError("atlas_future_time")
    return value  # Preserve the owner's collection time, including stale data.


def project_snapshot(body, now):
    if (
        body.get("schema_version") != 1
        or type(body.get("sources")) is not list
        or len(body["sources"]) > 9
    ):
        raise ValueError("atlas_schema")
    output = {"schema_version": 1, "server_now": now.isoformat(), "stale_after_seconds": 300}
    for key in ("received_at", "activity_received_at", "activity_publisher_at"):
        if key in body:
            output[key] = timestamp(body[key], now)
    for key in ("cycle_id", "activity_revision", "collector"):
        if key in body:
            output[key] = clean_text(body[key], 160)
    if body.get("state") == "waiting":
        output["state"] = "waiting"
    output["interval_seconds"] = 120
    seen, sources = set(), []
    for source in body["sources"]:
        identity = source["id"]
        if (
            identity not in SOURCE_IDS
            or identity in seen
            or source["status"] not in ("ok", "error")
        ):
            raise ValueError("atlas_source")
        seen.add(identity)
        checked = timestamp(source["checked_at"], now)
        success = timestamp(source.get("last_success_at"), now, optional=True)
        row = {
            "id": identity,
            "status": source["status"],
            "checked_at": checked,
            "last_success_at": success,
            "scope": clean_text(source.get("scope", ""), 400),
            "duration_ms": source.get("duration_ms", 0),
            "retained": source.get("retained") is True,
            "error_code": source.get("error_code"),
            "data": None,
        }
        if type(row["duration_ms"]) not in (int, float) or not 0 <= row["duration_ms"] <= 180000:
            raise ValueError("atlas_duration")
        if row["error_code"] is not None and row["error_code"] not in ERRORS:
            raise ValueError("atlas_error")
        if source.get("data") is not None:
            data = {}
            for kind, allowed in ROW_KEYS.items():
                rows = source["data"].get(kind, [])
                if type(rows) is not list or len(rows) > 1500:
                    raise ValueError("atlas_row_limit")
                data[kind] = [
                    {k: clean_text(item[k]) for k in allowed if k in item} for item in rows
                ]
            row["data"] = data
        if row["retained"] and (row["status"] != "error" or not success):
            raise ValueError("atlas_retained_time")
        sources.append(row)
    if sources and seen - {"target_host"} != SOURCE_IDS - {"target_host"}:
        raise ValueError("atlas_missing_source")
    if not sources and output.get("state") != "waiting":
        raise ValueError("atlas_empty_source")
    output["sources"] = sources
    activity = body.get("activity", [])
    if type(activity) is not list or len(activity) > 30:
        raise ValueError("atlas_activity_limit")
    output["activity"] = []
    for event in activity:
        if (
            event["stage"] not in STAGES
            or event["status"] not in STATUSES
            or event["reason_code"] not in REASONS
        ):
            raise ValueError("atlas_activity_type")
        output["activity"].append(
            {
                "stage": event["stage"],
                "status": event["status"],
                "reason_code": event["reason_code"],
                "run_id": clean_text(event["run_id"], 100),
                "receipt_id": clean_text(event["receipt_id"], 100),
                "observed_at": timestamp(event["observed_at"], now),
            }
        )
    output["activity_status"] = body.get("activity_status", "not_connected")
    if output["activity_status"] not in ("ok", "invalid", "not_connected"):
        raise ValueError("atlas_activity_status")
    return output


class AtlasClient:
    """Fixed public read endpoint. Never forwards the browser session upstream."""

    def __init__(self, origin):
        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or "\\" in origin
            or any(ord(c) < 33 for c in origin)
        ):
            raise ValueError("fixed_https_atlas_origin_required")
        self.origin = origin
        self.client = httpx.AsyncClient(
            base_url=origin,
            trust_env=False,
            follow_redirects=False,
            timeout=5,
            headers={"Accept-Encoding": "identity"},
        )
        self._cache, self._at, self._lock = None, 0, asyncio.Lock()

    async def read(self):
        async with self._lock:
            if self._cache is not None and monotonic() - self._at < 3:
                return self._cache
            async with asyncio.timeout(7), self.client.stream("GET", "/api/status") as response:
                response.raise_for_status()
                if (
                    response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ObservationError("atlas_response_type")
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 2097152:
                        raise ObservationError("atlas_response_limit")
                self._cache = object_document(raw, max_bytes=2097152)
                self._at = monotonic()
                return self._cache

    async def aclose(self):
        await self.client.aclose()


@dataclass(frozen=True)
class AtlasBinding:
    source_environment_id: str
    target_environment_id: str
    source_cluster: str = "source_k8s"
    target_cluster: str = "target_k8s"


class PlanNetworkAtlas:
    def __init__(self, api, client, *, authorize, registration):
        if not callable(authorize) or not callable(registration) or not callable(client.read):
            raise ValueError("explicit_network_registration_and_grant_required")
        self.api, self.client, self.authorize, self.registration = (
            api,
            client,
            authorize,
            registration,
        )

    async def read(self, scope, plan_id):
        before = await _plan(self.api, scope, plan_id)
        if await call(self.authorize, scope, dict(before)) is not True:
            raise ObservationError("forbidden", 403)
        binding = await call(self.registration, scope, dict(before))
        if (
            type(binding) is not AtlasBinding
            or (binding.source_environment_id, binding.target_environment_id)
            != (before["source_environment_id"], before["target_environment_id"])
            or binding.source_cluster != "source_k8s"
            or binding.target_cluster != "target_k8s"
        ):
            raise ObservationError("atlas_environment_unregistered")
        try:
            body = project_snapshot(
                await asyncio.wait_for(self.client.read(), 8), datetime.now(UTC)
            )
        except Exception:
            raise ObservationError("atlas_unavailable") from None
        after = await _plan(self.api, scope, plan_id)
        if before != after or await call(self.registration, scope, dict(after)) != binding:
            raise ObservationError("atlas_scope_changed")
        if await call(self.authorize, scope, dict(after)) is not True:
            raise ObservationError("forbidden", 403)
        body["sova_context"] = {
            "plan_id": plan_id,
            "plan_digest": after["plan_digest"],
            "plan_version": after["version"],
            "source_environment_id": binding.source_environment_id,
            "target_environment_id": binding.target_environment_id,
            "scope": "shared-environment-network",
            "authorizes_cutover": False,
        }
        return body

    async def stream(self, scope, receive, send, plan_id):
        websocket = WebSocket(scope, receive, send)
        http_scope = dict(scope, type="http", method="GET")
        disconnected = None
        try:
            # Authenticate and read the plan before accepting a persistent connection.
            await _plan(self.api, http_scope, plan_id)
            await websocket.accept()
            disconnected = asyncio.create_task(websocket.receive())
            deadline = monotonic() + 600
            last_revision, last_sent = None, 0
            while monotonic() < deadline:
                iteration_started = monotonic()
                try:
                    body = await self.read(http_scope, plan_id)
                    revision = hashlib.sha256(
                        json.dumps(
                            {k: v for k, v in body.items() if k != "server_now"}, sort_keys=True
                        ).encode()
                    ).hexdigest()
                    if revision != last_revision:
                        await websocket.send_json({"type": "snapshot", "data": body})
                        last_revision, last_sent = revision, monotonic()
                    elif monotonic() - last_sent >= 15:
                        await websocket.send_json({"type": "heartbeat"})
                        last_sent = monotonic()
                except ObservationError as error:
                    if error.status in (401, 403):
                        await websocket.close(code=4403)
                        return
                    await websocket.send_json({"type": "error", "code": "ATLAS_UNAVAILABLE"})
                    last_revision = None
                except Exception:
                    # Failed permission/registration services must not retain a trusted frame.
                    await websocket.send_json({"type": "error", "code": "ATLAS_UNAVAILABLE"})
                    last_revision = None
                done, _ = await asyncio.wait({disconnected}, timeout=3)
                if done:
                    message = disconnected.result()
                    if message["type"] == "websocket.disconnect":
                        return
                    disconnected = asyncio.create_task(websocket.receive())
                    # Client text is not a path, query, command or forwarding request.
                    await asyncio.sleep(max(0, 3 - (monotonic() - iteration_started)))
            await websocket.close(code=1000)
        except (WebSocketDisconnect, OSError, RuntimeError):
            return
        except ObservationError:
            await websocket.close(code=4403)
        finally:
            if disconnected is not None:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
