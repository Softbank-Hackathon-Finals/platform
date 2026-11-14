"""Decode retained OTLP JSON from an authenticated environment-local receiver.

This is not an ingestion endpoint or identity verifier. Registration and receipt
time must come from the receiver/store, never from a caller's uploaded document.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .audit_probes import AuditEvent, _constant, _hash, _json, _pairs, _utc

MAX_PAYLOAD = 1024 * 1024
MAX_RECORDS = 1000


@dataclass(frozen=True)
class AuditOrigin:
    environment: str
    registration_digest: str
    node_uid: str
    pod_uid: str
    namespace: str
    pod: str
    include_node_audit: bool = False

    def __post_init__(self):
        for value in (self.environment, self.namespace, self.pod):
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value
            ):
                raise ValueError("registered DNS label required")
        for value in (self.node_uid, self.pod_uid):
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("canonical source UID required")
        if (
            not isinstance(self.registration_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.registration_digest)
            or type(self.include_node_audit) is not bool
        ):
            raise ValueError("registered source binding required")

    @property
    def streams(self):
        streams = ("cnpg:" + self.pod_uid,)
        return streams + (("kubernetes:" + self.node_uid,) if self.include_node_audit else ())

    def attributes(self, kind):
        values = {
            "bravo.environment": self.environment,
            "bravo.audit.registration": self.registration_digest,
            "k8s.node.uid": self.node_uid,
            "bravo.audit.source": kind,
        }
        if kind == "cnpg":
            values.update(
                {
                    "k8s.namespace.name": self.namespace,
                    "k8s.pod.name": self.pod,
                    "k8s.pod.uid": self.pod_uid,
                    "k8s.container.name": "postgres",
                }
            )
        elif kind != "kubernetes" or not self.include_node_audit:
            raise ValueError("unregistered audit source kind")
        return values


def _map(values, depth=0):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("bounded OTLP key/value list required")
    result = {}
    for item in values:
        if not isinstance(item, dict) or set(item) != {"key", "value"}:
            raise ValueError("invalid OTLP key/value")
        key = item["key"]
        if not isinstance(key, str) or not key or len(key) > 256 or key in result:
            raise ValueError("duplicate or invalid OTLP key")
        result[key] = _value(item["value"], depth + 1)
    return result


def _value(value, depth=0):
    if depth > 12 or not isinstance(value, dict) or len(value) != 1:
        raise ValueError("bounded single OTLP AnyValue required")
    kind, content = next(iter(value.items()))
    if kind == "stringValue" and isinstance(content, str):
        return content
    if kind == "boolValue" and type(content) is bool:
        return content
    if (
        kind == "intValue"
        and isinstance(content, str)
        and re.fullmatch(r"-?(0|[1-9][0-9]{0,18})", content)
    ):
        number = int(content)
        if -(2**63) <= number < 2**63:
            return number
    if kind == "kvlistValue" and isinstance(content, dict) and set(content) <= {"values"}:
        return _map(content.get("values", []), depth)
    # Collector ParseJSON maps JSON numbers (e.g. Kubernetes status 201) to
    # doubleValue. Accept only lossless integral values within IEEE-754's exact
    # integer range; never round a fractional status into a success code.
    if kind == "doubleValue" and type(content) in {int, float} and abs(content) < 2**53:
        if int(content) == content:
            return int(content)
    # Audit fields do not require arbitrary bytes, fractional numbers or arrays. Unsupported
    # values fail the complete export rather than dropping a suspicious record.
    raise ValueError("unsupported OTLP audit value")


def _no_drops(value):
    dropped = value.get("droppedAttributesCount", 0)
    if type(dropped) is not int or dropped != 0:
        raise ValueError("truncated audit metadata")


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("nonzero OTLP event timestamp required")
    nanos = int(value)
    if nanos >= 2**64:
        raise ValueError("invalid OTLP timestamp")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=nanos // 1000)


def decode_export(payload: bytes, *, origin: AuditOrigin, received_at: datetime, receipt_id: str):
    """All-or-nothing decoding. The caller persists/queries whole exports.

    received_at is the trusted store acknowledgement time, not the sender's
    observedTimeUnixNano. receipt_id is a durable receiver identifier. The same
    retained export replay produces the same event IDs; separate export retries
    may contain repeated events and are not claimed as exactly-once ingestion.
    """
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError("bounded retained export required")
    if (
        not isinstance(origin, AuditOrigin)
        or not isinstance(receipt_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", receipt_id)
    ):
        raise ValueError("trusted origin and durable receipt required")
    received_at = _utc(received_at)
    try:
        document = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("invalid retained OTLP JSON") from None
    if not isinstance(document, dict) or set(document) != {"resourceLogs"}:
        raise ValueError("OTLP log export required")
    resources = document["resourceLogs"]
    if not isinstance(resources, list) or not 1 <= len(resources) <= 100:
        raise ValueError("bounded nonempty resource logs required")
    rows = []
    for resource_log in resources:
        if not isinstance(resource_log, dict) or not isinstance(resource_log.get("resource"), dict):
            raise ValueError("audit resource missing")
        resource = resource_log["resource"]
        _no_drops(resource)
        attributes = _map(resource.get("attributes"))
        kind = attributes.get("bravo.audit.source")
        if attributes != origin.attributes(kind):
            raise ValueError("OTLP origin differs from authenticated registration")
        stream = "cnpg:" + origin.pod_uid if kind == "cnpg" else "kubernetes:" + origin.node_uid
        scopes = resource_log.get("scopeLogs")
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 100:
            raise ValueError("bounded nonempty scopes required")
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get("scope", {}), dict):
                raise ValueError("invalid audit scope")
            _no_drops(scope.get("scope", {}))
            records = scope.get("logRecords")
            if not isinstance(records, list) or not records:
                raise ValueError("nonempty audit records required")
            for record in records:
                if not isinstance(record, dict) or len(rows) >= MAX_RECORDS:
                    raise ValueError("invalid or excessive audit records")
                _no_drops(record)
                if record.get("attributes", []) != []:
                    raise ValueError("unexpected unredacted audit attributes")
                body = _value(record.get("body"))
                if not isinstance(body, dict):
                    raise ValueError("structured audit body required")
                if kind == "kubernetes":
                    # filelog has no event time parser; Kubernetes supplies the
                    # API server's stage time in its retained structured body.
                    value = body.get("stageTimestamp")
                    if not isinstance(value, str):
                        raise ValueError("Kubernetes stage timestamp required")
                    timestamp = _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
                else:
                    timestamp = _timestamp(record.get("timeUnixNano"))
                if timestamp > received_at:
                    raise ValueError("event newer than trusted ingestion receipt")
                event_id = _hash(
                    _json(
                        {
                            "receipt": receipt_id,
                            "index": len(rows),
                            "registration": origin.registration_digest,
                        }
                    )
                )
                rows.append(
                    AuditEvent(event_id, stream, timestamp, received_at, _json(body).decode())
                )
    return tuple(rows)


@dataclass(frozen=True)
class AppLogOrigin:
    """One registered app container whose raw stdout/stderr the forwarder retains.

    SEC-01.3 needs an unredacted view, so the forwarding pipeline for this origin must
    not apply the audit privacy filter. The resource attributes bind the export to it.
    """

    environment: str
    registration_digest: str
    node_uid: str
    pod_uid: str
    namespace: str
    pod: str
    container: str

    def __post_init__(self):
        for value in (self.environment, self.namespace, self.pod, self.container):
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value
            ):
                raise ValueError("registered DNS label required")
        for value in (self.node_uid, self.pod_uid):
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("canonical source UID required")
        if not isinstance(self.registration_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.registration_digest
        ):
            raise ValueError("registered source binding required")

    @property
    def streams(self):
        return ("app:" + self.pod_uid,)

    def attributes(self):
        return {
            "bravo.environment": self.environment,
            "bravo.log.registration": self.registration_digest,
            "bravo.log.source": "app",
            "k8s.node.uid": self.node_uid,
            "k8s.namespace.name": self.namespace,
            "k8s.pod.name": self.pod,
            "k8s.pod.uid": self.pod_uid,
            "k8s.container.name": self.container,
        }


def decode_app_log_export(
    payload: bytes, *, origin: AppLogOrigin, received_at: datetime, receipt_id: str
):
    """All-or-nothing decoding of a retained OTLP JSON export of raw app log lines."""
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError("bounded retained export required")
    if not isinstance(origin, AppLogOrigin) or not isinstance(receipt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", receipt_id
    ):
        raise ValueError("trusted origin and durable receipt required")
    received_at = _utc(received_at)
    try:
        document = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("invalid retained OTLP JSON") from None
    if not isinstance(document, dict) or set(document) != {"resourceLogs"}:
        raise ValueError("OTLP log export required")
    resources = document["resourceLogs"]
    if not isinstance(resources, list) or not 1 <= len(resources) <= 100:
        raise ValueError("bounded nonempty resource logs required")
    rows = []
    for resource_log in resources:
        if not isinstance(resource_log, dict) or not isinstance(resource_log.get("resource"), dict):
            raise ValueError("log resource missing")
        _no_drops(resource_log["resource"])
        if _map(resource_log["resource"].get("attributes")) != origin.attributes():
            raise ValueError("OTLP origin differs from registered app")
        scopes = resource_log.get("scopeLogs")
        if not isinstance(scopes, list) or not 1 <= len(scopes) <= 100:
            raise ValueError("bounded nonempty scopes required")
        for scope in scopes:
            records = scope.get("logRecords") if isinstance(scope, dict) else None
            if not isinstance(records, list) or not records:
                raise ValueError("nonempty log records required")
            for record in records:
                if not isinstance(record, dict) or len(rows) >= MAX_RECORDS:
                    raise ValueError("invalid or excessive log records")
                _no_drops(record)
                body = _value(record.get("body"))
                message = body if isinstance(body, str) else _json(body).decode()
                timestamp = _timestamp(record.get("timeUnixNano"))
                if timestamp > received_at:
                    raise ValueError("event newer than trusted ingestion receipt")
                event_id = _hash(
                    _json(
                        {
                            "receipt": receipt_id,
                            "index": len(rows),
                            "registration": origin.registration_digest,
                        }
                    )
                )
                rows.append(AuditEvent(event_id, origin.streams[0], timestamp, received_at, message))
    return tuple(rows)
