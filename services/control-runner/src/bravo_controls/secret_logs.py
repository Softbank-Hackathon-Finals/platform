"""SEC-01.3: inspect a closed, registered canary log window without publishing logs.

Receipt creation and source qualification belong to the trusted host. This module
does not inject settings, restart applications, or accept user-supplied evidence.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .audit_probes import LOG_ARN, AuditEvent
from .runner import SHA256, Observation, RunContext, TestSpec

SPEC = TestSpec("SEC-01.3", "SEC-01", "1", "absent", "required")
MAX_EVENTS = 10_000
MAX_BYTES = 10 * 1024 * 1024
SECRET = re.compile(r"bravo_secret_[0-9a-f]{64}\Z")
MARKER = re.compile(r"bravo_log_[0-9a-f]{32}\Z")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware log timestamp required")
    return value.astimezone(UTC)


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _streams(value):
    if (not isinstance(value, tuple) or not 1 <= len(value) <= 100
            or any(not isinstance(s, str) or not 1 <= len(s) <= 512
                   or any(ord(c) < 32 for c in s) for s in value)
            or len(set(value)) != len(value)):
        raise ValueError("exact registered log streams required")


@dataclass(frozen=True, repr=False)
class LogCanaryReceipt:
    context: RunContext
    source_binding: str
    streams: tuple[str, ...]
    configuration_sha256: str
    secret: str = field(repr=False)
    marker: str = field(repr=False)
    started_at: datetime
    ended_at: datetime
    # Host confirms delivery/drain through ended_at, not just an elapsed timer.
    settled_at: datetime
    acknowledged: frozenset[str]

    def __post_init__(self):
        _streams(self.streams)
        if (not isinstance(self.context, RunContext)
                or not isinstance(self.source_binding, str) or not self.source_binding
                or len(self.source_binding) > 2048
                or not isinstance(self.configuration_sha256, str)
                or not SHA256.fullmatch(self.configuration_sha256)
                or not isinstance(self.secret, str) or not SECRET.fullmatch(self.secret)
                or not isinstance(self.marker, str) or not MARKER.fullmatch(self.marker)
                or not isinstance(self.acknowledged, frozenset)
                or self.acknowledged != {"configured", "exercised", "restored", "logs_drained"}
                or not _utc(self.started_at) < _utc(self.ended_at) <= _utc(self.settled_at)
                or self.settled_at - self.started_at > timedelta(minutes=10)):
            raise ValueError("completed registered log canary required")


@dataclass(frozen=True, repr=False)
class LogWindow:
    context: RunContext
    source_binding: str
    streams: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    queried_at: datetime
    events: tuple[AuditEvent, ...] = field(repr=False)
    complete: bool
    # An unfiltered, unmasked source is essential; a redacted view cannot prove absence.
    raw_view: bool


class SecretLogProbe:
    def __init__(self, *, receipt_provider, search, source_binding, streams, configuration_sha256,
                 evidence_sink, clock=lambda: datetime.now(UTC)):
        _streams(streams)
        if (not all(callable(c) for c in (receipt_provider, search, evidence_sink, clock))
                or not isinstance(source_binding, str) or not source_binding
                or len(source_binding) > 2048 or not isinstance(configuration_sha256, str)
                or not SHA256.fullmatch(configuration_sha256)):
            raise ValueError("trusted log collector required")
        self.receipts, self.search, self.sink, self.clock = receipt_provider, search, evidence_sink, clock
        self.source, self.streams = source_binding, streams
        self.configuration = configuration_sha256

    def __call__(self, context, spec):
        if not isinstance(context, RunContext) or spec != SPEC:
            raise ValueError("log canary method mismatch")
        receipt = self.receipts(context, spec)
        before = _utc(self.clock())
        if (not isinstance(receipt, LogCanaryReceipt) or receipt.context != context
                or receipt.source_binding != self.source or receipt.streams != self.streams
                or receipt.configuration_sha256 != self.configuration
                or not receipt.settled_at <= before <= receipt.settled_at + timedelta(minutes=10)):
            raise ValueError("current execution log receipt required")
        batch = self.search(receipt)
        collected = _utc(self.clock())
        if (not isinstance(batch, LogWindow) or batch.context != context
                or batch.source_binding != self.source or batch.streams != self.streams
                or batch.started_at != receipt.started_at or batch.ended_at != receipt.ended_at
                or not before <= _utc(batch.queried_at) <= collected
                or batch.complete is not True or batch.raw_view is not True
                or not isinstance(batch.events, tuple) or len(batch.events) > MAX_EVENTS):
            raise ValueError("complete unmasked log window required")
        seen, positives, event_hashes = set(), set(), []
        size, leaks = 0, 0
        for event in batch.events:
            if (not isinstance(event, AuditEvent) or not isinstance(event.message, str)
                    or not isinstance(event.id, str) or not 1 <= len(event.id) <= 512
                    or event.stream not in self.streams or (event.stream, event.id) in seen
                    or not receipt.started_at <= _utc(event.timestamp) <= receipt.ended_at
                    or not event.timestamp <= _utc(event.ingested_at) <= batch.queried_at):
                raise ValueError("invalid or duplicate canary log event")
            seen.add((event.stream, event.id))
            raw = event.message.encode("utf-8")
            size += len(raw)
            if size > MAX_BYTES:
                raise ValueError("canary log budget exceeded")
            if receipt.marker in event.message:
                positives.add(event.stream)
            leaks += receipt.secret in event.message
            # Commit to the complete searched envelopes, never persist their text.
            event_hashes.append(_hash(json.dumps({
                "id": event.id, "stream": event.stream, "message": event.message,
                "timestamp": event.timestamp.isoformat(), "ingested_at": event.ingested_at.isoformat(),
            }, sort_keys=True, separators=(",", ":")).encode()))
        positive = positives == set(self.streams)
        observed = ("present" if leaks else "absent") if positive else None
        reason = None if positive else "POSITIVE_CONTROL_FAILED"
        payload = json.dumps({
            "format_version": "1", "test_id": SPEC.test_id, "method_version": SPEC.method_version,
            "run_id": context.run_id, "app": context.app,
            "environment": {"id": context.environment.id, "role": context.environment.role,
                            "ref": context.environment.ref},
            "source_sha256": _hash(self.source.encode()),
            "configuration_sha256": receipt.configuration_sha256,
            "secret_sha256": _hash(receipt.secret.encode()), "marker_sha256": _hash(receipt.marker.encode()),
            "started_at": receipt.started_at.isoformat(), "ended_at": receipt.ended_at.isoformat(),
            "settled_at": receipt.settled_at.isoformat(), "collected_at": collected.isoformat(),
            "streams": len(self.streams), "positive_streams": len(positives),
            "searched_events": len(seen), "leaking_events": leaks,
            "event_sha256": sorted(event_hashes), "observed": observed, "unknown_reason": reason,
        }, sort_keys=True, separators=(",", ":")).encode()
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "pass" if positive else "fail",
                           collected, "bravo-secret-log-search", "1", _hash(payload), reason)


class CloudWatchSecretLogSearch:
    """Read only, explicit Unmask permission, all pages and no text filter.

AWS FilterLogEvents can return empty pages with nextToken. Pagination completion
does not prove collector delivery; the host must separately produce logs_drained.
https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html
"""

    def __init__(self, client, *, log_group_arn, streams,
                 clock=lambda: datetime.now(UTC), monotonic=time.monotonic):
        _streams(streams)
        match = LOG_ARN.fullmatch(log_group_arn) if isinstance(log_group_arn, str) else None
        if not match:
            raise ValueError("registered log group required")
        self.client, self.source, self.streams = client, log_group_arn, streams
        self.region, self.clock, self.monotonic = match[2], clock, monotonic
        self._validate()

    def _validate(self):
        from .aws_probes import _client

        _client(self.client, "logs", self.region)

    def __call__(self, receipt):
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        self._validate()
        if (not isinstance(receipt, LogCanaryReceipt) or receipt.source_binding != self.source
                or receipt.streams != self.streams or receipt.settled_at > _utc(self.clock())
                or receipt.started_at.microsecond % 1000 or receipt.ended_at.microsecond % 1000):
            raise ValueError("registered settled log window required")
        query = {"logGroupIdentifier": self.source, "logStreamNames": list(self.streams),
                 "startTime": int(receipt.started_at.timestamp() * 1000),
                 "endTime": int(receipt.ended_at.timestamp() * 1000), "unmask": True, "limit": 1000}
        rows, tokens, size = [], set(), 0
        deadline = self.monotonic() + 30
        for _ in range(10):
            if self.monotonic() >= deadline:
                raise TimeoutError("log query budget exceeded")
            try:
                page = self.client.filter_log_events(**query)
            except ClientError as error:
                if (error.response.get("Error", {}).get("Code") in {"AccessDenied", "AccessDeniedException"}
                        and error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {400, 403}):
                    raise PermissionError("log query denied") from None
                raise ValueError("log query failed") from None
            except (ConnectTimeoutError, ReadTimeoutError):
                raise TimeoutError("log query timed out") from None
            if self.monotonic() >= deadline:
                raise TimeoutError("log query budget exceeded")
            if (not isinstance(page, dict) or page.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    or not isinstance(page.get("events"), list)):
                raise ValueError("invalid log query response")
            for event in page["events"]:
                if (not isinstance(event, dict) or not isinstance(event.get("message"), str)
                        or not isinstance(event.get("eventId"), str)
                        or event.get("logStreamName") not in self.streams
                        or type(event.get("timestamp")) is not int
                        or type(event.get("ingestionTime")) is not int):
                    raise ValueError("invalid log event envelope")
                size += len(event["message"].encode())
                if size > MAX_BYTES or len(rows) >= MAX_EVENTS:
                    raise ValueError("log query output limit exceeded")
                rows.append(AuditEvent(event["eventId"], event["logStreamName"],
                                       datetime.fromtimestamp(event["timestamp"] / 1000, UTC),
                                       datetime.fromtimestamp(event["ingestionTime"] / 1000, UTC),
                                       event["message"]))
            token = page.get("nextToken")
            if token is None:
                return LogWindow(receipt.context, self.source, self.streams, receipt.started_at,
                                 receipt.ended_at, _utc(self.clock()), tuple(rows), True, True)
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or token in tokens:
                raise ValueError("invalid log pagination")
            tokens.add(token)
            query["nextToken"] = token
        raise ValueError("log query page limit exceeded")
