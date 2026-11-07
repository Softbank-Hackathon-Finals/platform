"""Read-only, plan-bound workload projections. Never an execution authorization.

The host registers the producer and verifies its provenance. The browser cannot
select a producer, set a trusted revision, or submit synthetic response evidence.
"""

import asyncio
import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from .observations import ObservationError, _plan
from .wire import object_document
from .workspace_data import call
from .workspace_reports import SECRET

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
STAGES = {
    "detect": "security",
    "hold": "hold",
    "approve": "approval",
    "isolate": "isolation",
    "replace": "replacement",
    "verify": "security",
    "route": "route",
    "recover": "recovery",
}
STATUSES = {"pending", "running", "requested", "verified", "failed", "unknown"}


def require(condition):
    if not condition:
        raise ValueError("invalid_workload_projection")


def stamp(value):
    require(type(value) is str and bool(re.search(r"(?:Z|[+-]\d\d:\d\d)$", value)))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.utcoffset() is not None)
    return result


def safe_text(value, limit=160):
    require(type(value) is str and 0 < len(value) <= limit)
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    # A producer must project public descriptions, not return arbitrary logs.
    # Reject the whole projection on common credential formats; do not silently
    # replace an identifier and accidentally break its evidence binding.
    require(not SECRET.search(value))
    return value


def identifier(value):
    require(type(value) is str and bool(ID.fullmatch(value)))
    return value


def fields(row, names):
    require(type(row) is dict and set(row) == set(names.split()))


def validate_projection(doc, plan, now):
    fields(
        doc,
        "kind authorizes_execution plan_id plan_digest plan_version revision stream_id provenance source_label observed_at expires_at nodes edges evidence responses events",
    )
    require(doc["kind"] == "console-workload.v1" and doc["authorizes_execution"] is False)
    identifier(doc["plan_id"])
    require(type(doc["plan_digest"]) is str and bool(HASH.fullmatch(doc["plan_digest"])))
    require(type(doc["plan_version"]) is int and 0 < doc["plan_version"] <= 2**53 - 1)
    require(
        (doc["plan_id"], doc["plan_digest"], doc["plan_version"])
        == (plan["plan_id"], plan["plan_digest"], plan["version"])
    )
    require(type(doc["revision"]) is int and 0 < doc["revision"] <= 2**53 - 1)
    identifier(doc["stream_id"])
    require(doc["provenance"] in ("live", "synthetic", "historical"))
    safe_text(doc["source_label"])
    observed, expiry = stamp(doc["observed_at"]), stamp(doc["expires_at"])
    require(observed <= now and 0 < (expiry - observed).total_seconds() <= 300)
    for key, limit in (
        ("nodes", 48),
        ("edges", 96),
        ("evidence", 128),
        ("responses", 8),
        ("events", 100),
    ):
        require(
            type(doc[key]) is list
            and len(doc[key]) <= limit
            and all(type(r) is dict for r in doc[key])
        )
    require(bool(doc["nodes"]))
    for key in ("nodes", "edges", "evidence", "events"):
        require(len({identifier(r.get("id")) for r in doc[key]}) == len(doc[key]))
    require(
        len({r["id"] for r in doc["nodes"] + doc["edges"]}) == len(doc["nodes"]) + len(doc["edges"])
    )
    nodes = {r["id"]: r for r in doc["nodes"]}
    evidence = {r["id"]: r for r in doc["evidence"]}
    for e in doc["evidence"]:
        fields(e, "id resource_id resource_uid kind outcome summary observed_at expires_at")
        identifier(e["resource_id"])
        safe_text(e["resource_uid"])
        safe_text(e["summary"], 240)
        require(
            e["kind"]
            in {
                "health",
                "security",
                "isolation",
                "replacement",
                "route",
                "recovery",
                "approval",
                "hold",
            }
        )
        require(e["outcome"] in {"verified", "failed", "unknown"})
        require(
            stamp(e["observed_at"]) <= observed
            and stamp(e["observed_at"]) < stamp(e["expires_at"]) <= expiry
        )

    def proof(proof_id, resource, kind):
        e = evidence.get(proof_id)
        return (
            e
            and e["resource_id"] == resource["id"]
            and e["resource_uid"] == resource["resource_uid"]
            and e["kind"] == kind
            and e["outcome"] == "verified"
        )

    for n in doc["nodes"]:
        fields(
            n,
            "id label resource_uid detail kind lane column row health security health_evidence security_evidence",
        )
        safe_text(n["label"], 80)
        safe_text(n["resource_uid"])
        safe_text(n["detail"], 240)
        require(
            n["kind"]
            in {
                "edge",
                "app",
                "worker",
                "database",
                "secret",
                "security",
                "executor",
                "storage",
                "queue",
                "identity",
                "ai",
                "observability",
            }
        )
        require(n["lane"] in {"source", "control", "target"})
        require(
            type(n["column"]) is int
            and 0 <= n["column"] <= 4
            and type(n["row"]) is int
            and 0 <= n["row"] <= 9
        )
        require(n["health"] in {"unknown", "ready", "degraded", "unavailable"})
        require(n["security"] in {"unknown", "clear", "warning", "hold", "isolated"})
        require(n["health"] == "unknown" or proof(n["health_evidence"], n, "health"))
        kind = {
            "clear": "security",
            "warning": "security",
            "hold": "hold",
            "isolated": "isolation",
        }.get(n["security"])
        require(kind is None or proof(n["security_evidence"], n, kind))
        for key in ("health_evidence", "security_evidence"):
            require(n[key] is None or type(n[key]) is str and n[key] in evidence)
    for edge in doc["edges"]:
        fields(edge, "id source target label kind resource_uid state evidence_id")
        require(
            edge["source"] in nodes and edge["target"] in nodes and edge["source"] != edge["target"]
        )
        safe_text(edge["label"], 80)
        safe_text(edge["resource_uid"])
        require(edge["kind"] in {"http", "database", "control", "dependency", "replication"})
        require(edge["state"] in {"unknown", "observed", "requested", "blocked"})
        kind = {"blocked": "isolation", "observed": "health"}.get(edge["state"])
        require(kind is None or proof(edge["evidence_id"], edge, kind))
        require(
            edge["evidence_id"] is None
            or type(edge["evidence_id"]) is str
            and edge["evidence_id"] in evidence
        )
    require(len({r.get("stage") for r in doc["responses"]}) == len(doc["responses"]))
    for r in doc["responses"]:
        fields(r, "stage status detail node_ids evidence_id")
        require(r["stage"] in STAGES and r["status"] in STATUSES)
        safe_text(r["detail"], 240)
        require(type(r["node_ids"]) is list and len(r["node_ids"]) <= 48)
        require(
            all(type(n) is str and n in nodes for n in r["node_ids"])
            and len(set(r["node_ids"])) == len(r["node_ids"])
        )
        require(
            r["evidence_id"] is None
            or type(r["evidence_id"]) is str
            and r["evidence_id"] in evidence
        )
        require(
            r["status"] != "verified"
            or (
                len(r["node_ids"]) == 1
                and proof(r["evidence_id"], nodes[r["node_ids"][0]], STAGES[r["stage"]])
            )
        )
    sequence = 0
    for e in doc["events"]:
        fields(e, "id sequence occurred_at stage status summary")
        require(type(e["sequence"]) is int and sequence < e["sequence"] <= 2**53 - 1)
        require(
            stamp(e["occurred_at"]) <= observed and e["stage"] in STAGES and e["status"] in STATUSES
        )
        safe_text(e["summary"], 240)
        sequence = e["sequence"]
    return doc


@dataclass(frozen=True, repr=False)
class RegisteredWorkload:
    producer_id: str
    source_revision: str
    content_sha256: str
    document: bytes


class PlanWorkload:
    """Uses T10 session/tenant access and a separately registered read authority.

    authenticate must verify the producer's trusted envelope/signature or server
    registry. Returning True merely because a JSON claims provenance is invalid.
    No runtime default, discovery, write client, or credential fallback exists.
    """

    def __init__(
        self,
        api,
        *,
        load,
        authorize,
        authenticate,
        producer_id,
        source_revision,
        provenance="live",
        clock=None,
        timeout=5,
    ):
        identifier(producer_id)
        require(type(source_revision) is str and bool(SHA.fullmatch(source_revision)))
        require(provenance in {"live", "synthetic", "historical"})
        require(all(callable(f) for f in (load, authorize, authenticate)))
        require(type(timeout) in (int, float) and 0 < timeout <= 5)
        self.api, self.load, self.authorize, self.authenticate = api, load, authorize, authenticate
        self.producer_id, self.source_revision, self.provenance = (
            producer_id,
            source_revision,
            provenance,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout = timeout
        # A timed-out blocking provider can still be running. Reserve capacity
        # until that read really finishes, rather than spawning unlimited threads.
        self.capacity = threading.BoundedSemaphore(4)

    async def read(self, scope, plan_id):
        if not self.capacity.acquire(blocking=False):
            raise ObservationError("workload_busy")
        task = asyncio.create_task(self._read(scope, plan_id))

        def finished(done):
            self.capacity.release()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except ObservationError:
            raise
        except Exception:
            # Never include provider exceptions, log excerpts or credential values.
            raise ObservationError("workload_unavailable_or_unverified") from None

    async def _read(self, scope, plan_id):
        before = await _plan(self.api, scope, plan_id)
        if await call(self.authorize, scope, dict(before)) is not True:
            raise ObservationError("workload_forbidden", 403)
        value = await call(self.load, scope, dict(before))
        require(type(value) is RegisteredWorkload)
        require(
            value.producer_id == self.producer_id and value.source_revision == self.source_revision
        )
        require(type(value.document) is bytes and len(value.document) <= 120000)
        require(hashlib.sha256(value.document).hexdigest() == value.content_sha256)
        require(await call(self.authenticate, scope, dict(before), value) is True)
        doc = validate_projection(
            object_document(value.document, max_bytes=120000), before, self.clock()
        )
        require(doc["provenance"] == self.provenance)
        # Freeze the plan identity across an asynchronous provider read. T10 owns
        # tenant and plan authority; this adapter does not mint a second one.
        after = await _plan(self.api, scope, plan_id)
        keys = (
            "plan_id",
            "plan_digest",
            "version",
            "source_environment_id",
            "target_environment_id",
            "workload_id",
        )
        require(all(before.get(k) == after.get(k) for k in keys))
        if await call(self.authorize, scope, dict(after)) is not True:
            raise ObservationError("workload_forbidden", 403)
        require(await call(self.authenticate, scope, dict(after), value) is True)
        validate_projection(doc, after, self.clock())
        # Expired evidence can be shown as history, but never refresh its expiry.
        return doc
