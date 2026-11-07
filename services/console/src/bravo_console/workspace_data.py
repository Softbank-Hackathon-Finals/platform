"""Plan-bound read adapters for the console's supplementary panels.

Providers are server registrations, not browser-supplied paths, URLs or data.
A report is presentation evidence and never an authorization for a mutation.
"""

import asyncio
import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .observations import ENV, ID, FixedJsonClient, ObservationError, _plan, http_observation_row
from .wire import object_document
from .workspace_reports import FEATURES, PROJECTORS, table, text


async def call(fn, *args):
    result = fn(*args) if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn, *args)
    return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True, repr=False)
class RegisteredPanel:
    plan_id: str
    plan_digest: str
    plan_version: int
    feature: str
    source_revision: str
    content_sha256: str
    expires_at: str
    document: bytes


class RegisteredPanelSource:
    def __init__(self, feature, *, load, authenticate, clock=None):
        if feature not in FEATURES or not callable(load) or not callable(authenticate):
            raise ValueError("registered_panel_authority_required")
        self.feature, self.load, self.authenticate = feature, load, authenticate
        self.clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, scope, plan):
        value = await call(self.load, scope, dict(plan))
        if (
            type(value) is not RegisteredPanel
            or type(value.plan_version) is not int
            or (value.plan_id, value.plan_digest, value.plan_version, value.feature)
            != (plan["plan_id"], plan["plan_digest"], plan["version"], self.feature)
            or type(value.document) is not bytes
            or hashlib.sha256(value.document).hexdigest() != value.content_sha256
            or len(value.source_revision) != 40
            or any(c not in "0123456789abcdef" for c in value.source_revision)
            or datetime.fromisoformat(value.expires_at) <= self.clock()
            or await call(self.authenticate, scope, dict(plan), value) is not True
        ):
            raise ObservationError("panel_provenance_unverified")
        document = object_document(value.document, max_bytes=2097152)
        tables = PROJECTORS[self.feature](document, plan)
        if (
            datetime.fromisoformat(value.expires_at) <= self.clock()
            or await call(self.authenticate, scope, dict(plan), value) is not True
        ):
            raise ObservationError("panel_authority_changed")
        return {
            "tables": tables,
            "source_revision": value.source_revision,
            "content_digest": value.content_sha256,
            "valid_until": value.expires_at,
        }


class T37Details:
    """Existing fixed T37 routes, restricted to the plan's registered pair."""

    def __init__(self, client: FixedJsonClient, *, environments):
        if (
            not isinstance(client, FixedJsonClient)
            or type(environments) is not dict
            or not all(ID.fullmatch(k) and ENV.fullmatch(v) for k, v in environments.items())
            or len(set(environments.values())) != len(environments)
        ):
            raise ValueError("registered_observation_mapping_required")
        self.client, self.environments = client, dict(environments)

    async def __call__(self, scope, plan):
        output = {}
        mapped = []
        for side in ("source", "target"):
            environment = plan[side + "_environment_id"]
            name = self.environments.get(environment)
            if name is None:
                raise ObservationError("monitoring_unconfigured")
            mapped.append(name)
            output[side] = {"environment_id": environment}

        async def read(side, route):
            name = self.environments[plan[side + "_environment_id"]]
            try:
                value = await self.client.read_json(
                    f"/v1/observability/environments/{name}/{route}"
                )
                stamp = datetime.fromisoformat(value["queried_at"].replace("Z", "+00:00"))
                age = (datetime.now(UTC) - stamp).total_seconds()
                if value.get("environment") != name or not 0 <= age < 30:
                    raise ValueError("observation_binding_invalid")
                if route == "http":
                    checked = http_observation_row(value, name, now=datetime.now(UTC))
                    value = checked | {
                        "last_received_seconds_ago": value.get("last_received_seconds_ago")
                        if checked["state"] == "OK"
                        else None
                    }
                output[side][route] = value
            except Exception:
                output[side][route] = {"state": "UNOBSERVABLE"}

        await asyncio.gather(
            *(
                read(side, route)
                for side in ("source", "target")
                for route in ("http", "database", "pgbouncer")
            )
        )
        try:
            alert = await self.client.read_json("/v1/observability/alerts")
            stamp = datetime.fromisoformat(alert["queried_at"].replace("Z", "+00:00"))
            if not 0 <= (datetime.now(UTC) - stamp).total_seconds() < 30:
                raise ValueError("stale_alerts")
            output["alerts"] = [row for row in alert["alerts"] if row["environment"] in mapped]
        except Exception:
            output["alerts"] = []
            # Never turn an unavailable alert source into a clean bill of health.
            output["alerts_state"] = "UNOBSERVABLE"
        return {
            "tables": PROJECTORS["monitoring"](output, plan),
            "alerts_state": output.get("alerts_state", "OK"),
        }


class AuditPanel:
    def __init__(self, reader):
        from .audit import PlanAudit

        if not isinstance(reader, PlanAudit):
            raise ValueError("verified_t24_reader_required")
        self.reader = reader

    async def __call__(self, scope, plan):
        doc = await self.reader.read(scope, plan["plan_id"])
        return {"tables": PROJECTORS["audit"](doc, plan), "content_digest": doc["chain_digest"]}


class PlanWorkspace:
    def __init__(self, api, *, sources=None, authorize, operations=None, clock=None):
        sources = {} if sources is None else sources
        if (
            set(sources) - set(FEATURES)
            or not all(callable(s) for s in sources.values())
            or not callable(authorize)
        ):
            raise ValueError("explicit_workspace_sources_and_permissions_required")
        self.api, self.sources, self.authorize = api, dict(sources), authorize
        self.operations = operations
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _feature(self, scope, plan, name):
        try:
            permitted = await asyncio.wait_for(call(self.authorize, scope, dict(plan), name), 3)
        except Exception:
            return {"state": "UNKNOWN", "reason": "permission_unavailable", "tables": []}
        if permitted is not True:
            return {"state": "FORBIDDEN", "reason": "not_authorized", "tables": []}
        source = self.sources.get(name)
        if source is None:
            return {"state": "UNCONFIGURED", "reason": "source_unconfigured", "tables": []}
        try:
            result = await asyncio.wait_for(call(source, scope, dict(plan)), timeout=6)
            if await call(self.authorize, scope, dict(plan), name) is not True:
                return {"state": "FORBIDDEN", "reason": "permission_changed", "tables": []}
            if type(result) is not dict or type(result.get("tables")) is not list:
                raise ValueError("invalid_workspace_projection")
            # Restrict the envelope too: raw upstream data and private diagnostics never escape.
            clean = {
                k: result[k]
                for k in (
                    "tables",
                    "source_revision",
                    "content_digest",
                    "alerts_state",
                    "valid_until",
                )
                if k in result
            }
            if len(clean["tables"]) > 10:
                raise ValueError("workspace_table_limit")
            normalized = []
            for block in clean["tables"]:
                if (
                    type(block) is not dict
                    or type(block.get("columns")) is not list
                    or not 1 <= len(block["columns"]) <= 12
                    or type(block.get("rows")) is not list
                    or any(
                        type(row) is not list or len(row) != len(block["columns"])
                        for row in block["rows"]
                    )
                ):
                    raise ValueError("invalid_workspace_table")
                normalized.append(
                    table(
                        text(block["title"], 160),
                        [text(c, 80) for c in block["columns"]],
                        block["rows"],
                    )
                )
            clean["tables"] = normalized
            if "source_revision" in clean and not re.fullmatch(
                "[a-f0-9]{40}", clean["source_revision"]
            ):
                raise ValueError("invalid_source_revision")
            if "content_digest" in clean and not re.fullmatch(
                "[a-f0-9]{64}", clean["content_digest"]
            ):
                raise ValueError("invalid_content_digest")
            if len(json.dumps(clean).encode()) > 50000:
                raise ValueError("workspace_feature_limit")
            return {"state": "OK", **clean}
        except Exception:
            return {"state": "UNKNOWN", "reason": "source_unavailable_or_unverified", "tables": []}

    async def read(self, scope, plan_id):
        before = await _plan(self.api, scope, plan_id)
        results = await asyncio.gather(*(self._feature(scope, before, name) for name in FEATURES))
        operations = None
        if self.operations is not None:
            try:
                operations = await asyncio.wait_for(self.operations.context(scope, before), 6)
            except ObservationError as error:
                if error.status == 403:
                    raise
            except Exception:
                operations = None
        after = await _plan(self.api, scope, plan_id)
        if before != after:
            raise ObservationError("workspace_scope_changed")
        # A per-feature permission may have been revoked while another provider was slow.
        for name, result in zip(FEATURES, results, strict=True):
            if result["state"] == "OK":
                try:
                    allowed = await asyncio.wait_for(
                        call(self.authorize, scope, dict(after), name), 3
                    )
                except Exception:
                    result.clear()
                    result.update(state="UNKNOWN", reason="permission_unavailable", tables=[])
                else:
                    if allowed is not True:
                        result.clear()
                        result.update(state="FORBIDDEN", reason="permission_changed", tables=[])
        now = self.clock()
        for result in results:
            if result["state"] == "OK" and "valid_until" in result:
                try:
                    expired = datetime.fromisoformat(result["valid_until"]) <= now
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    result.clear()
                    result.update(state="UNKNOWN", reason="source_expired", tables=[])
        value = {
            "kind": "console-workspace.v1",
            "plan_id": plan_id,
            "plan_digest": after["plan_digest"],
            "plan_version": after["version"],
            "queried_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=15)).isoformat(),
            "authorizes_cutover": False,
            "features": dict(zip(FEATURES, results, strict=True)),
            "operations": operations,
        }
        if len(json.dumps(value).encode()) > 120000:
            raise ObservationError("workspace_response_limit")
        return value
