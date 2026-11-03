"""T35 read-only restore qualification, not a backup/cutover orchestrator.

Receipts and clients are supplied by trusted execution/registration. JSON from a
user or model is not proof of a backup, immutable data boundary, or fresh restore.
The caller authorizes effects; this module never starts backups or deletes data.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .runner import Observation, RunContext, TestSpec

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware receipt timestamp required")
    return value.astimezone(UTC)


def _ids(*values):
    if any(not isinstance(v, str) or not ID.fullmatch(v) for v in values):
        raise ValueError("registered resource identity required")


def _hash(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class MarkerBoundary:
    """T5's append-only path: older writers drained and old rows immutable.

    These are trusted executor attestations bound to retained evidence, not
    switches the requester may set. Without that evidence, do not construct this
    receipt. Sequence allocation alone does not prove commit order.
    """

    context: RunContext
    source_uid: str
    database: str
    marker_id: int
    marker_sha256: str
    row_count: int
    recorded_at: datetime
    boundary_evidence_sha256: str

    def __post_init__(self):
        _ids(self.source_uid, self.database)
        if (
            not isinstance(self.context, RunContext)
            or type(self.marker_id) is not int
            or not 1 <= self.marker_id <= 2**63 - 1
            or type(self.row_count) is not int
            or not 1 <= self.row_count <= self.marker_id
            or not isinstance(self.marker_sha256, str)
            or not SHA.fullmatch(self.marker_sha256)
            or not isinstance(self.boundary_evidence_sha256, str)
            or not SHA.fullmatch(self.boundary_evidence_sha256)
        ):
            raise ValueError("retained, consistent source marker boundary required")
        _time(self.recorded_at)


@dataclass(frozen=True)
class CompletedBackup:
    context: RunContext
    source_uid: str
    backup_uid: str
    backup_id: str
    archive_binding_sha256: str
    requested_at: datetime
    started_at: datetime
    completed_at: datetime

    def __post_init__(self):
        _ids(self.source_uid, self.backup_uid, self.backup_id)
        if (
            not isinstance(self.context, RunContext)
            or not isinstance(self.archive_binding_sha256, str)
            or not SHA.fullmatch(self.archive_binding_sha256)
            or not _time(self.requested_at).replace(microsecond=0)
            <= _time(self.started_at)
            <= _time(self.completed_at)
            or self.requested_at > self.completed_at
        ):
            raise ValueError("completed bound backup required")


@dataclass(frozen=True)
class RestoredDatabase:
    context: RunContext
    source_uid: str
    target_uid: str
    backup_uid: str
    backup_id: str
    archive_binding_sha256: str
    database: str
    created_at: datetime
    ready_at: datetime
    observed_at: datetime
    marker_id: int | None
    marker_sha256: str | None
    row_count: int

    def __post_init__(self):
        _ids(self.source_uid, self.target_uid, self.backup_uid, self.backup_id, self.database)
        if (
            not isinstance(self.context, RunContext)
            or self.source_uid == self.target_uid
            or not isinstance(self.archive_binding_sha256, str)
            or not SHA.fullmatch(self.archive_binding_sha256)
            or not _time(self.created_at) <= _time(self.ready_at) <= _time(self.observed_at)
            or type(self.row_count) is not int
            or self.row_count < 0
            or ((self.marker_id is None) != (self.marker_sha256 is None))
            or (
                self.marker_id is not None
                and (
                    type(self.marker_id) is not int
                    or not 1 <= self.marker_id <= 2**63 - 1
                    or not isinstance(self.marker_sha256, str)
                    or not SHA.fullmatch(self.marker_sha256)
                )
            )
        ):
            raise ValueError("fresh restore readback required")


class RestoreProbe:
    """DR-01.1: compare an actual readback against a frozen source boundary.

    Providers must return recorded execution receipts and perform a fresh query
    of the exact restored cluster. Missing/error/permission-denied inputs propagate
    to Runner's UNKNOWN; a complete read with mismatched data is failure.
    """

    def __init__(
        self,
        *,
        boundary_provider,
        backup_provider,
        restore_reader,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if not all(
            callable(f)
            for f in (boundary_provider, backup_provider, restore_reader, evidence_sink, clock)
        ):
            raise ValueError("trusted providers and evidence sink required")
        self.boundary, self.backup, self.reader = boundary_provider, backup_provider, restore_reader
        self.sink, self.clock = evidence_sink, clock

    def __call__(self, context, spec):
        if spec != TestSpec("DR-01.1", "DR-01", "1", "success", "not_applicable"):
            raise ValueError("restore method mismatch")
        started = _time(self.clock())
        boundary = self.boundary(context)
        backup = self.backup(context)
        if not isinstance(boundary, MarkerBoundary) or not isinstance(backup, CompletedBackup):
            raise ValueError("source and backup receipts required")
        if (
            boundary.context != context
            or backup.context != context
            or boundary.source_uid != backup.source_uid
            or not boundary.recorded_at <= backup.requested_at <= backup.completed_at <= started
            or started - boundary.recorded_at > timedelta(hours=24)
        ):
            raise ValueError("backup is not bound to this current marker boundary")
        restored = self.reader(context, backup, boundary)
        finished = _time(self.clock())
        if (
            not isinstance(restored, RestoredDatabase)
            or restored.context != context
            or restored.source_uid != boundary.source_uid
            or restored.database != boundary.database
            or restored.backup_uid != backup.backup_uid
            or restored.backup_id != backup.backup_id
            or restored.archive_binding_sha256 != backup.archive_binding_sha256
            or restored.created_at < backup.completed_at
            or not started <= restored.observed_at <= finished
        ):
            raise ValueError("restore provenance, identity or readback freshness mismatch")
        equal = (
            restored.marker_id == boundary.marker_id
            and restored.marker_sha256 == boundary.marker_sha256
            and restored.row_count == boundary.row_count
        )
        payload = _json(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "source_uid_sha256": _hash(boundary.source_uid.encode()),
                "target_uid_sha256": _hash(restored.target_uid.encode()),
                "backup_uid_sha256": _hash(backup.backup_uid.encode()),
                "backup_id_sha256": _hash(backup.backup_id.encode()),
                "archive_binding_sha256": backup.archive_binding_sha256,
                "boundary_evidence_sha256": boundary.boundary_evidence_sha256,
                "expected_marker_sha256": boundary.marker_sha256,
                "restored_marker_sha256": restored.marker_sha256,
                "marker_id_equal": restored.marker_id == boundary.marker_id,
                "expected_rows": boundary.row_count,
                "restored_rows": restored.row_count,
                "backup_requested_at": backup.requested_at.isoformat(),
                "backup_started_at": backup.started_at.isoformat(),
                "backup_completed_at": backup.completed_at.isoformat(),
                "restored_at": restored.ready_at.isoformat(),
                "collected_at": finished.isoformat(),
                "observed": "success" if equal else "failure",
                "qualification": "marker-and-count-only",
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            "1",
            "success" if equal else "failure",
            "not_applicable",
            finished,
            "bravo-restore-qualification",
            "1",
            _hash(payload),
        )
