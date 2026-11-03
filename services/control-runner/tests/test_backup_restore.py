import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.backup_restore import (
    CompletedBackup,
    MarkerBoundary,
    RestoredDatabase,
    RestoreProbe,
)
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
CTX = RunContext("restore-run-1", "demo-chat", Environment("target", "target", "k8s:target"))
HASH = "sha256:" + "a" * 64
SPEC = Spec("DR-01.1", "DR-01", "1", "success", "not_applicable")


def receipts():
    boundary = MarkerBoundary(
        CTX, "source-uid", "chat", 20, HASH, 19, NOW - timedelta(minutes=10), HASH
    )
    backup = CompletedBackup(
        CTX,
        "source-uid",
        "backup-uid",
        "20261102T235100",
        HASH,
        NOW - timedelta(minutes=9),
        NOW - timedelta(minutes=9),
        NOW - timedelta(minutes=5),
    )
    restored = RestoredDatabase(
        CTX,
        "source-uid",
        "new-target-uid",
        "backup-uid",
        backup.backup_id,
        HASH,
        "chat",
        NOW - timedelta(minutes=4),
        NOW - timedelta(minutes=1),
        NOW,
        20,
        HASH,
        19,
    )
    return boundary, backup, restored


def probe(boundary=None, backup=None, restored=None, sink=None):
    b, k, r = receipts()
    evidence = []
    return RestoreProbe(
        boundary_provider=lambda c: boundary or b,
        backup_provider=lambda c: backup or k,
        restore_reader=lambda c, k, b: restored or r,
        evidence_sink=sink or evidence.append,
        clock=lambda: NOW,
    ), evidence


def test_actual_restore_readback_required_and_evidence_minimized():
    p, evidence = probe()
    assert p(CTX, SPEC).observed == "success"
    saved = json.loads(evidence[0])
    assert saved["expected_rows"] == 19
    assert saved["qualification"] == "marker-and-count-only"
    assert b"source-uid" not in evidence[0] and b"new-target-uid" not in evidence[0]


@pytest.mark.parametrize(
    "change",
    [
        {"marker_id": 21},
        {"marker_sha256": "sha256:" + "b" * 64},
        {"row_count": 18},
        {"marker_id": None, "marker_sha256": None},
    ],
)
def test_complete_mismatched_data_is_failure(change):
    restored = replace(receipts()[2], **change)
    p, evidence = probe(restored=restored)
    assert p(CTX, SPEC).observed == "failure"
    assert evidence


@pytest.mark.parametrize(
    "change",
    [
        {"context": replace(CTX, run_id="other-run")},
        {"source_uid": "other-source"},
        {"backup_uid": "other-backup"},
        {"backup_id": "old-backup"},
        {"archive_binding_sha256": "sha256:" + "b" * 64},
        {"database": "other-db"},
        {"created_at": NOW - timedelta(minutes=6)},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_wrong_restore_provenance_never_emits_evidence(change):
    p, evidence = probe(restored=replace(receipts()[2], **change))
    with pytest.raises(ValueError):
        p(CTX, SPEC)
    assert evidence == []


@pytest.mark.parametrize(
    "change",
    [
        {"context": replace(CTX, app="other-app")},
        {"source_uid": "other-source"},
        {"requested_at": NOW - timedelta(minutes=11)},
        {"completed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_wrong_backup_or_pre_marker_backup_rejected(change):
    p, evidence = probe(backup=replace(receipts()[1], **change))
    with pytest.raises(ValueError):
        p(CTX, SPEC)
    assert evidence == []


@pytest.mark.parametrize(
    "change",
    [
        {"marker_id": True},
        {"marker_id": 0},
        {"row_count": 0},
        {"row_count": True},
        {"row_count": 21},
        {"marker_sha256": ""},
        {"boundary_evidence_sha256": ""},
        {"recorded_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_boundary_rejected(change):
    with pytest.raises(ValueError):
        replace(receipts()[0], **change)


def test_old_boundary_is_unknown_not_clean_restore():
    p, evidence = probe(boundary=replace(receipts()[0], recorded_at=NOW - timedelta(days=2)))
    with pytest.raises(ValueError):
        p(CTX, SPEC)
    assert evidence == []


def test_second_precision_provider_time_needs_precise_request_order():
    boundary, backup, _ = receipts()
    requested = backup.started_at + timedelta(microseconds=800000)
    boundary = replace(boundary, recorded_at=backup.started_at + timedelta(microseconds=500000))
    backup = replace(backup, requested_at=requested)
    p, _ = probe(boundary=boundary, backup=backup)
    assert p(CTX, SPEC).observed == "success"
    p, evidence = probe(
        boundary=replace(boundary, recorded_at=requested + timedelta(microseconds=1)), backup=backup
    )
    with pytest.raises(ValueError):
        p(CTX, SPEC)
    assert evidence == []


def test_provider_start_cannot_predate_request_by_a_whole_second():
    backup = receipts()[1]
    with pytest.raises(ValueError):
        replace(backup, requested_at=backup.started_at + timedelta(seconds=1))


@pytest.mark.parametrize(
    "change",
    [
        {"target_uid": "source-uid"},
        {"row_count": True},
        {"row_count": -1},
        {"marker_id": True},
        {"marker_id": None},
        {"marker_sha256": None},
        {"ready_at": NOW - timedelta(minutes=6)},
        {"observed_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_restore_shape_rejected(change):
    with pytest.raises(ValueError):
        replace(receipts()[2], **change)


def test_sink_failure_cannot_publish_success():
    def fail(_):
        raise OSError("storage unavailable")

    p, _ = probe(sink=fail)
    with pytest.raises(OSError):
        p(CTX, SPEC)


def test_permission_denied_reader_is_not_data_failure():
    p, evidence = probe()

    def fail(*args):
        raise PermissionError("private provider detail")

    p.reader = fail
    with pytest.raises(PermissionError):
        p(CTX, SPEC)
    assert evidence == []
