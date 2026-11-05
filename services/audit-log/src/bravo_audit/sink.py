"""Where records are kept. ``ObjectLockSink`` is the production path (S3 Object Lock,
COMPLIANCE retention, write-once keys); ``FileSink`` is the local reference that refuses
overwrites so tests and demos cannot silently rewrite history."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .record import AuditRecord, canonical


class UnverifiedWrite(RuntimeError):
    """The store did not confirm a new, retained version. The write must not be reported as kept."""


@dataclass(frozen=True)
class Receipt:
    key: str
    sha256: str
    version_id: str
    retain_until: str | None
    retention_mode: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def object_key(record: AuditRecord, prefix: str = "audit") -> str:
    return f"{prefix}/{record.plan_id}/{record.run_id}/{record.sequence:08d}.json"


class Sink(Protocol):
    def put(self, record: AuditRecord) -> Receipt: ...


class FileSink:
    def __init__(self, root: Path, prefix: str = "audit"):
        self.root, self.prefix = Path(root), prefix

    def put(self, record: AuditRecord) -> Receipt:
        key = object_key(record, self.prefix)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical(record.as_dict())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return Receipt(key, record.sha256, "file", None, None)


class ObjectLockSink:
    """Writes one object per record with ``If-None-Match: *`` (no overwrite, no new version on an
    existing key) and an explicit COMPLIANCE retention. A write counts only if the store returns a
    VersionId and, when readback is possible, reports the retention we asked for."""

    def __init__(self, client: Any, bucket: str, *, retention_days: int = 7, prefix: str = "audit", now=None):
        if retention_days < 1:
            raise ValueError("retention must be at least one day")
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.retention = timedelta(days=retention_days)
        self.now = now or (lambda: datetime.now(UTC))

    def put(self, record: AuditRecord) -> Receipt:
        key = object_key(record, self.prefix)
        data = canonical(record.as_dict())
        retain_until = self.now() + self.retention
        response = self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType="application/json",
            IfNoneMatch="*", ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain_until,
            ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
        )
        version_id = response.get("VersionId") if isinstance(response, dict) else None
        if not version_id or version_id == "null":
            raise UnverifiedWrite("store returned no version id; bucket versioning/Object Lock not active")
        # The receipt reports what the store confirmed, never what was requested. Readback is not
        # optional: a receipt without it would be consumed as a confirmed COMPLIANCE retention.
        head = self.client.head_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        mode = head.get("ObjectLockMode")
        observed = head.get("ObjectLockRetainUntilDate")
        if mode != "COMPLIANCE" or observed is None or observed < retain_until - timedelta(seconds=1):
            raise UnverifiedWrite("readback did not confirm COMPLIANCE retention")
        until = observed.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(observed, "strftime") else str(observed)
        return Receipt(key, record.sha256, version_id, until, mode)
