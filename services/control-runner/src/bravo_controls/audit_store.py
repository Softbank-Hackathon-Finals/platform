"""T34 target audit store search (ADR-0016) → AuditBatch for AuditSearchProbe.

The store is a versioned Object Lock (COMPLIANCE) bucket of OTLP JSON exports written by the
environment's audit forwarder. This adapter uses the read/list-only identity and fails closed:
it lists every version under the minute partitions covering the window, treats a key with more
than one version or any delete marker as tamper evidence, and refuses truncated or over-budget
listings. received_at is LastModified + 1 s because the store truncates to seconds; receipt_id
binds the key and version. Like the CloudWatch search, only events inside the canary window
that contain the marker term are returned.
"""

import hashlib
import re
import time
from datetime import UTC, datetime, timedelta

from .audit_probes import MAX_BYTES, MAX_EVENTS, AuditBatch, CanaryReceipt, _utc
from .otlp_audit import (
    MAX_PAYLOAD,
    AppLogOrigin,
    AuditOrigin,
    decode_app_log_export,
    decode_export,
)
from .secret_logs import LogCanaryReceipt, LogWindow

BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
PREFIX = re.compile(r"[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*\Z")
# awss3 exporter: s3_partition_format '%Y/%m/%d/%H/%M' (UTC), unique_key_func_name uuidv7, otlp_json
OBJECT = re.compile(r"logs_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json\Z")
VERSION = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
MAX_OBJECTS = 1000
MAX_PAGES = 50
BUDGET_SECONDS = 30


def _minutes(start, end):
    minute = start.replace(second=0, microsecond=0)
    while minute <= end:
        yield minute
        minute += timedelta(minutes=1)


class ObjectStoreAuditSearch:
    """One registered forwarder origin and its exact bucket prefix; no write or delete calls."""

    def __init__(
        self,
        client,
        *,
        bucket: str,
        prefix: str,
        region: str,
        origin: AuditOrigin,
        skew=timedelta(minutes=1),
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        endpoint_url: str | None = None,
    ):
        if (
            not isinstance(bucket, str)
            or not BUCKET.fullmatch(bucket)
            or not isinstance(prefix, str)
            or not PREFIX.fullmatch(prefix)
            or not isinstance(origin, self._origin_type)
            or not isinstance(skew, timedelta)
            or not timedelta(0) <= skew <= timedelta(minutes=2)
        ):
            raise ValueError("registered audit bucket, prefix and origin required")
        self.client, self.bucket, self.prefix, self.region = client, bucket, prefix, region
        self.origin, self.skew = origin, skew
        # Optional pin of the S3-compatible store (onprem SeaweedFS) endpoint.
        self.endpoint_url = endpoint_url
        self.source_binding = f"s3://{bucket}/{prefix}/"
        self.streams = origin.streams
        self.clock, self.monotonic = clock, monotonic
        self._validate_client()

    _origin_type = AuditOrigin

    def _validate_client(self):
        from .aws_probes import _client

        _client(self.client, "s3", self.region, endpoint_url=self.endpoint_url)

    def _call(self, operation, started, **params):
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        if self.monotonic() - started >= BUDGET_SECONDS:
            raise TimeoutError("audit search budget exceeded")
        try:
            response = getattr(self.client, operation)(**params)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"AccessDenied", "AccessDeniedException"} and status == 403:
                raise PermissionError("audit search denied") from None
            raise ValueError("audit store search failed") from None
        except (ConnectTimeoutError, ReadTimeoutError):
            raise TimeoutError("audit store timed out") from None
        if self.monotonic() - started >= BUDGET_SECONDS:
            raise TimeoutError("audit search budget exceeded")
        if not isinstance(response, dict) or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise ValueError("invalid audit store response")
        return response

    def _versions(self, partition, started):
        """Every version and delete marker under one minute partition, or an error."""
        params = {"Bucket": self.bucket, "Prefix": partition, "MaxKeys": 1000}
        versions, seen = [], set()
        for _ in range(MAX_PAGES):
            page = self._call("list_object_versions", started, **params)
            if page.get("DeleteMarkers"):
                raise ValueError("audit store tamper evidence: delete marker")
            rows = page.get("Versions", [])
            if not isinstance(rows, list):
                raise ValueError("audit store listing lacks versions")
            versions.extend(rows)
            truncated = page.get("IsTruncated")
            marker = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker"))
            # Only an explicit "not truncated" without a next marker ends the listing.
            if truncated is False and marker == (None, None):
                return versions
            if truncated is not True:
                raise ValueError("audit store listing end is missing or contradicts its markers")
            if not all(isinstance(m, str) and m for m in marker) or marker in seen:
                raise ValueError("invalid or repeated audit store pagination")
            seen.add(marker)
            params.update(KeyMarker=marker[0], VersionIdMarker=marker[1])
        raise ValueError("audit store listing incomplete")

    def __call__(self, receipt: CanaryReceipt, through: datetime) -> AuditBatch:
        self._validate_client()
        through = _utc(through)
        if not isinstance(receipt, CanaryReceipt) or not receipt.started_at <= through <= receipt.deadline:
            raise ValueError("search interval outside authorized canary window")
        events, queried_at = self._collect(
            receipt.started_at,
            through,
            through,
            lambda event: receipt.marker in event.message,
        )
        return AuditBatch(
            receipt.context,
            receipt.marker,
            self.source_binding,
            self.streams,
            receipt.started_at,
            through,
            queried_at,
            events,
            True,
        )

    def _decode(self, payload, received, receipt_id):
        return decode_export(payload, origin=self.origin, received_at=received, receipt_id=receipt_id)

    def _collect(self, window_start, window_end, through, keep):
        """Retained events with window_start <= timestamp <= window_end acknowledged by through."""
        started = self.monotonic()
        # Partitions are named by the writer's clock at upload; widen by the allowed skew.
        objects = {}
        for minute in _minutes(window_start - self.skew, through + self.skew):
            partition = f"{self.prefix}/{minute:%Y/%m/%d/%H/%M}/"
            for row in self._versions(partition, started):
                key, version = row.get("Key"), row.get("VersionId")
                if (
                    not isinstance(key, str)
                    or not key.startswith(partition)
                    or not OBJECT.fullmatch(key[len(partition):])
                    or not isinstance(version, str)
                    or not VERSION.fullmatch(version)
                    or version == "null"
                    or type(row.get("Size")) is not int
                    or not 1 <= row["Size"] <= MAX_PAYLOAD
                    or not isinstance(row.get("LastModified"), datetime)
                ):
                    raise ValueError("unexpected object in audit store partition")
                if key in objects:
                    raise ValueError("audit store tamper evidence: object has several versions")
                objects[key] = row
                if len(objects) > MAX_OBJECTS:
                    raise ValueError("audit store search exceeds object budget")
        events, size = [], 0
        earliest_retention = None
        for key in sorted(objects):
            row = objects[key]
            received = _utc(row["LastModified"]) + timedelta(seconds=1)
            if received > through:
                continue  # acknowledged after this search's horizon; a later poll reads it
            body = self._call("get_object", started, Bucket=self.bucket, Key=key, VersionId=row["VersionId"])
            retain_until = body.get("ObjectLockRetainUntilDate")
            if (
                body.get("VersionId") != row["VersionId"]
                or body.get("LastModified") != row["LastModified"]
                or body.get("ObjectLockMode") != "COMPLIANCE"
                or body.get("ContentLength") != row["Size"]
            ):
                raise ValueError("audit store object differs from listing or is not retained")
            # A lapsed COMPLIANCE retention still reports the mode; only a version retained when the
            # result is fixed counts, so keep the earliest retention and compare it once at queried_at.
            if not isinstance(retain_until, datetime):
                raise ValueError("audit store object retention missing or expired")
            retain_until = _utc(retain_until)
            if earliest_retention is None or retain_until < earliest_retention:
                earliest_retention = retain_until
            payload = body["Body"].read(MAX_PAYLOAD + 1)
            if len(payload) != row["Size"]:
                raise ValueError("audit store object size mismatch")
            receipt_id = hashlib.sha256(f"{key}\0{row['VersionId']}".encode()).hexdigest()
            for event in self._decode(payload, received, receipt_id):
                if not window_start <= event.timestamp <= window_end or not keep(event):
                    continue
                size += len(event.message.encode())
                if len(events) >= MAX_EVENTS or size > MAX_BYTES:
                    raise ValueError("audit response exceeds budget")
                events.append(event)
        queried_at = _utc(self.clock())
        if earliest_retention is not None and earliest_retention <= queried_at:
            raise ValueError("audit store object retention missing or expired")
        return tuple(events), queried_at


class ObjectStoreSecretLogSearch(ObjectStoreAuditSearch):
    """SEC-01.3 onprem: raw app logs from the same retained OTLP object store (T34 path).

    Reuses the audit store's listing, tamper and retention checks, but decodes an
    ``AppLogOrigin`` export and returns every event in the closed canary window with no
    text filter, so SecretLogProbe sees both the positive marker and any leaked secret.
    The forwarder for this origin must retain raw (unfiltered) app log lines.
    """

    _origin_type = AppLogOrigin

    def _decode(self, payload, received, receipt_id):
        return decode_app_log_export(
            payload, origin=self.origin, received_at=received, receipt_id=receipt_id
        )

    def __call__(self, receipt: LogCanaryReceipt) -> LogWindow:
        self._validate_client()
        now = _utc(self.clock())
        if (
            not isinstance(receipt, LogCanaryReceipt)
            or receipt.source_binding != self.source_binding
            or receipt.streams != self.streams
            or receipt.settled_at > now
        ):
            raise ValueError("registered settled log window required")
        # The host's logs_drained acknowledgement (settled_at) bounds the store horizon.
        events, queried_at = self._collect(
            receipt.started_at, receipt.ended_at, receipt.settled_at, lambda _: True
        )
        return LogWindow(
            receipt.context,
            self.source_binding,
            self.streams,
            receipt.started_at,
            receipt.ended_at,
            queried_at,
            events,
            True,
            True,
        )
