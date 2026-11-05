"""T34 bounded audit searches → T14 observations, never raw log publication.

This is a trusted worker library, not an uploaded-evidence API. The caller binds
canary receipts to authorized, successful effects and authenticated log sources.
It must not register an AI proposal or user JSON as a receipt/search provider.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .runner import Observation, RunContext, TestSpec

TESTS = {"AUD-01.1": {"login"}, "AUD-01.2": {"create", "delete"}, "AUD-01.3": {"create", "delete"}}
MAX_EVENTS = 10_000
MAX_BYTES = 10 * 1024 * 1024
TOKEN = re.compile(r"bravo_audit_[0-9a-f]{32}\Z")
NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,62}\Z")
LOG_ARN = re.compile(
    r"arn:(aws|aws-cn|aws-us-gov):logs:([a-z0-9-]+):([0-9]{12}):log-group:([A-Za-z0-9_./#-]{1,512})\Z"
)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return value.astimezone(UTC)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit field")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("invalid JSON number")


@dataclass(frozen=True)
class CanaryReceipt:
    context: RunContext
    test_id: str
    marker: str
    actor: str
    database: str
    namespace: str
    schema: str
    started_at: datetime
    completed_at: datetime
    deadline: datetime
    # Set by the trusted effect executor after acknowledgements; not a user claim.
    acknowledged: frozenset[str]

    def __post_init__(self):
        if (
            not isinstance(self.context, RunContext)
            or self.test_id not in TESTS
            or not isinstance(self.marker, str)
            or not TOKEN.fullmatch(self.marker)
            or not isinstance(self.actor, str)
            or not 1 <= len(self.actor) <= 256
            or any(ord(c) < 32 for c in self.actor)
            or any(
                not isinstance(x, str) or not NAME.fullmatch(x)
                for x in (self.database, self.namespace, self.schema)
            )
            or not isinstance(self.acknowledged, frozenset)
            or self.acknowledged != TESTS[self.test_id]
            or not _utc(self.started_at) <= _utc(self.completed_at) < _utc(self.deadline)
            or not timedelta(0) < self.deadline - self.started_at <= timedelta(minutes=10)
        ):
            raise ValueError("successful registered canary receipt required")


@dataclass(frozen=True, repr=False)
class AuditEvent:
    id: str
    stream: str
    timestamp: datetime
    ingested_at: datetime
    message: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class AuditBatch:
    context: RunContext
    marker: str
    source_binding: str
    streams: tuple[str, ...]
    started_at: datetime
    through: datetime
    queried_at: datetime
    events: tuple[AuditEvent, ...] = field(repr=False)
    complete: bool


def _matched(event: dict, receipt: CanaryReceipt) -> str | None:
    """Exact semantic fields, never substring matches of SQL/request bodies."""
    if not isinstance(event, dict):
        raise ValueError("audit object required")
    if receipt.test_id == "AUD-01.3":
        ref, response, user = (
            event.get("objectRef", {}),
            event.get("responseStatus", {}),
            event.get("user", {}),
        )
        if not all(isinstance(x, dict) for x in (ref, response, user)):
            raise ValueError("invalid Kubernetes audit shape")
        verb = event.get("verb")
        if (
            event.get("apiVersion") != "audit.k8s.io/v1"
            or event.get("kind") != "Event"
            or event.get("stage") != "ResponseComplete"
            or verb not in {"create", "delete"}
            or user.get("username") != receipt.actor
            or event.get("impersonatedUser")
            or ref.get("apiGroup") != "rbac.authorization.k8s.io"
            or ref.get("resource") != "rolebindings"
            or ref.get("subresource")
            or ref.get("namespace") != receipt.namespace
            or ref.get("name") != receipt.marker
            or type(response.get("code")) is not int
            or not 200 <= response["code"] < 300
        ):
            return None
        # Kubernetes also records successful server dry-runs with HTTP 2xx.
        # They are not evidence of a committed RBAC change. Successful effect
        # receipts remain mandatory; Metadata-only logs cannot prove effects.
        uri = event.get("requestURI")
        # The existing privacy-filtered OTLP path removes URIs entirely and
        # therefore still relies on the trusted executor's committed receipts.
        # When raw provider logs include a URI, never ignore contradictory data.
        if uri is not None:
            if not isinstance(uri, str) or not 1 <= len(uri) <= 4096:
                raise ValueError("audit request URI invalid")
            request = urlsplit(uri)
            path = f"/apis/rbac.authorization.k8s.io/v1/namespaces/{receipt.namespace}/rolebindings"
            if verb == "delete":
                path += "/" + receipt.marker
            if request.scheme or request.netloc or request.fragment or request.path != path:
                return None
            query = parse_qsl(request.query, keep_blank_values=True, max_num_fields=32)
            if any(key == "dryRun" for key, _ in query):
                return None
        body = event.get("requestObject")
        if isinstance(body, dict) and body.get("dryRun"):
            return None
        audit_id = event.get("auditID")
        if not isinstance(audit_id, str) or not audit_id:
            raise ValueError("audit request identifier missing")
        timestamp = _utc(datetime.fromisoformat(event["stageTimestamp"].replace("Z", "+00:00")))
        if not receipt.started_at <= timestamp <= receipt.deadline:
            return None
        return verb
    record = event.get("record", {})
    if not isinstance(record, dict):
        raise ValueError("invalid CNPG record")
    if record.get("user_name") != receipt.actor or record.get("database_name") != receipt.database:
        return None
    if receipt.test_id == "AUD-01.1":
        message = record.get("message")
        return (
            "login"
            if (
                event.get("logger") == "postgres"
                and record.get("error_severity") == "LOG"
                and isinstance(message, str)
                # PostgreSQL can log authorization before application_name has
                # reached the structured CSV field. Match the exact trusted
                # server message grammar, never arbitrary SQL/text substrings.
                and record.get("application_name") in {None, "", receipt.marker}
                and re.fullmatch(
                    r"connection authorized: user="
                    + re.escape(receipt.actor)
                    + r" database="
                    + re.escape(receipt.database)
                    + r" application_name="
                    + re.escape(receipt.marker)
                    + r"(?: SSL enabled \(protocol=[A-Za-z0-9.]+, "
                    r"cipher=[A-Za-z0-9_-]+, bits=[0-9]+\))?",
                    message,
                )
            )
            else None
        )
    audit = record.get("audit", {})
    if not isinstance(audit, dict):
        raise ValueError("invalid pgAudit record")
    if (
        event.get("logger") != "pgaudit"
        or record.get("application_name") != receipt.marker
        or audit.get("audit_type") != "SESSION"
        or audit.get("class") != "DDL"
        or audit.get("object_type") != "TABLE"
        or audit.get("object_name") != f"{receipt.schema}.{receipt.marker}"
    ):
        return None
    return {"CREATE TABLE": "create", "DROP TABLE": "delete"}.get(audit.get("command"))


class AuditSearchProbe:
    """Caller polls until the receipt deadline; this class does not sleep.

    Before the deadline missing events are NOT_RUN, not an audit failure. After
    it, a complete successful search with no required event is absent/FAIL.
    Errors/partial results remain UNKNOWN even when a matching event was seen.
    """

    def __init__(
        self,
        *,
        receipt_provider,
        search,
        source_binding: str,
        streams: tuple[str, ...],
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not all(callable(x) for x in (receipt_provider, search, evidence_sink, clock))
            or not isinstance(source_binding, str)
            or not source_binding
            or not isinstance(streams, tuple)
            or not 1 <= len(streams) <= 100
            or len(set(streams)) != len(streams)
            or any(not isinstance(s, str) or not s or len(s) > 512 for s in streams)
        ):
            raise ValueError("registered audit source required")
        self.receipts, self.search = receipt_provider, search
        self.source_binding, self.streams = source_binding, streams
        self.sink, self.clock = evidence_sink, clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if spec.test_id not in TESTS or spec != TestSpec(
            spec.test_id, "AUD-01", "1", "present", "not_applicable"
        ):
            raise ValueError("audit method mismatch")
        receipt = self.receipts(context, spec)
        now = _utc(self.clock())
        if (
            not isinstance(receipt, CanaryReceipt)
            or receipt.context != context
            or receipt.test_id != spec.test_id
            or receipt.completed_at > now
            or now - receipt.deadline > timedelta(minutes=10)
        ):
            raise ValueError("current execution canary receipt required")
        batch = self.search(receipt, min(now, receipt.deadline))
        collected = _utc(self.clock())
        if (
            not isinstance(batch, AuditBatch)
            or batch.context != context
            or batch.marker != receipt.marker
            or batch.source_binding != self.source_binding
            or batch.streams != self.streams
            or batch.started_at != receipt.started_at
            or batch.through != min(now, receipt.deadline)
            or not now <= _utc(batch.queried_at) <= collected
            or batch.complete is not True
            or not isinstance(batch.events, tuple)
            or len(batch.events) > MAX_EVENTS
        ):
            raise ValueError("incomplete or mismatched audit search")
        found, hashes, seen = set(), [], set()
        size = 0
        for row in batch.events:
            if not isinstance(row, AuditEvent) or not isinstance(row.message, str):
                raise ValueError("invalid audit event")
            size += len(row.message.encode())
            if size > MAX_BYTES or not row.id or (row.stream, row.id) in seen:
                raise ValueError("duplicate or excessive audit events")
            seen.add((row.stream, row.id))
            if (
                row.stream not in self.streams
                or not receipt.started_at <= _utc(row.timestamp) <= batch.through
                or not row.timestamp <= _utc(row.ingested_at) <= batch.queried_at
            ):
                raise ValueError("audit source or time mismatch")
            document = json.loads(row.message, object_pairs_hook=_pairs, parse_constant=_constant)
            action = _matched(document, receipt)
            if action is not None:
                # Late ingestion cannot prove that the ten-minute search SLO was met.
                if row.ingested_at <= receipt.deadline:
                    found.add(action)
                hashes.append(
                    _hash(
                        _json(
                            {
                                "id": row.id,
                                "stream": row.stream,
                                "message": row.message,
                                "timestamp": row.timestamp.isoformat(),
                                "ingested_at": row.ingested_at.isoformat(),
                            }
                        )
                    )
                )
        ready = found >= TESTS[spec.test_id]
        observed = "present" if ready else ("absent" if now >= receipt.deadline else None)
        reason = None if observed is not None else "NOT_RUN"
        payload = _json(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "method_version": "1",
                "marker_sha256": _hash(receipt.marker.encode()),
                "source_sha256": _hash(self.source_binding.encode()),
                "required_actions": sorted(TESTS[spec.test_id]),
                "found_actions": sorted(found),
                "matching_event_sha256": sorted(hashes),
                "searched_events": len(seen),
                "started_at": receipt.started_at.isoformat(),
                "deadline": receipt.deadline.isoformat(),
                "collected_at": collected.isoformat(),
                "observed": observed,
                "unknown_reason": reason,
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            "1",
            observed,
            "not_applicable",
            collected,
            "bravo-audit-search",
            "1",
            _hash(payload),
            reason,
        )


class CloudWatchAuditSearch:
    """Exact registered streams; full pagination; no unmask permission required."""

    def __init__(
        self,
        client,
        *,
        log_group_arn: str,
        streams: tuple[str, ...],
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        match = LOG_ARN.fullmatch(log_group_arn) if isinstance(log_group_arn, str) else None
        if (
            not match
            or not isinstance(streams, tuple)
            or not 1 <= len(streams) <= 100
            or len(set(streams)) != len(streams)
            or any(
                not isinstance(s, str) or not 1 <= len(s) <= 512 or any(ord(c) < 32 for c in s)
                for s in streams
            )
        ):
            raise ValueError("registered log group and exact stream set required")
        self.client, self.arn, self.streams = client, log_group_arn, streams
        self.region = match[2]
        self.clock, self.monotonic = clock, monotonic
        self._validate_client()

    def _validate_client(self):
        from .aws_probes import _client

        _client(self.client, "logs", self.region)

    def __call__(self, receipt: CanaryReceipt, through: datetime) -> AuditBatch:
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        self._validate_client()
        through = _utc(through)
        if not receipt.started_at <= through <= receipt.deadline:
            raise ValueError("search interval outside authorized canary window")
        query = {
            "logGroupIdentifier": self.arn,
            "logStreamNames": list(self.streams),
            "filterPattern": '"' + receipt.marker + '"',
            "unmask": False,
            "limit": 1000,
            "startTime": int(receipt.started_at.timestamp() * 1000),
            "endTime": int(through.timestamp() * 1000),
        }
        rows, tokens = [], set()
        started = self.monotonic()
        size = 0
        for _ in range(10):
            if self.monotonic() - started >= 30:
                raise TimeoutError("audit search budget exceeded")
            try:
                page = self.client.filter_log_events(**query)
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in {"AccessDenied", "AccessDeniedException"} and status in {400, 403}:
                    raise PermissionError("audit search denied") from None
                raise ValueError("audit provider search failed") from None
            except (ConnectTimeoutError, ReadTimeoutError):
                raise TimeoutError("audit provider timed out") from None
            if self.monotonic() - started >= 30:
                raise TimeoutError("audit search budget exceeded")
            if (
                not isinstance(page, dict)
                or page.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            ):
                raise ValueError("invalid audit search response")
            events = page.get("events")
            if not isinstance(events, list):
                raise ValueError("audit page lacks events")
            for event in events:
                if (
                    not isinstance(event, dict)
                    or not isinstance(event.get("message"), str)
                    or not isinstance(event.get("eventId"), str)
                    or not event["eventId"]
                    or event.get("logStreamName") not in self.streams
                    or type(event.get("timestamp")) is not int
                    or type(event.get("ingestionTime")) is not int
                ):
                    raise ValueError("invalid audit event envelope")
                size += len(event["message"].encode())
                if len(rows) >= MAX_EVENTS or size > MAX_BYTES:
                    raise ValueError("audit response exceeds budget")
                rows.append(
                    AuditEvent(
                        event["eventId"],
                        event["logStreamName"],
                        datetime.fromtimestamp(event["timestamp"] / 1000, UTC),
                        datetime.fromtimestamp(event["ingestionTime"] / 1000, UTC),
                        event["message"],
                    )
                )
            token = page.get("nextToken")
            if token is None:
                queried = _utc(self.clock())
                return AuditBatch(
                    receipt.context,
                    receipt.marker,
                    self.arn,
                    self.streams,
                    receipt.started_at,
                    through,
                    queried,
                    tuple(rows),
                    True,
                )
            if not isinstance(token, str) or not token or len(token) > 10000 or token in tokens:
                raise ValueError("invalid or repeated audit pagination token")
            tokens.add(token)
            query["nextToken"] = token
        raise ValueError("audit pagination incomplete")
