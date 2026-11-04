from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bravo_controls.backup_restore import CompletedBackup, MarkerBoundary
from bravo_controls.cnpg_readback import resource_fingerprint
from bravo_controls.cnpg_restore_readback import CNPGRestoreReader, RestoreRequestBinding
from bravo_controls.postgres_restore import MarkerReadback
from bravo_controls.runner import Environment, RunContext

NOW = datetime(2026, 11, 4, 1, tzinfo=UTC)
CTX = RunContext("restore-trial", "chat", Environment("source", "source", "local:test"))
UID = [f"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee{i}" for i in range(4)]
SHA = "sha256:" + "a" * 64
MARKER = "11111111-2222-4333-8444-55555555555a"


def setup(monkeypatch):
    backup = CompletedBackup(
        CTX,
        UID[0],
        UID[1],
        "20261104T005000",
        SHA,
        NOW - timedelta(minutes=10),
        NOW - timedelta(minutes=9),
        NOW - timedelta(minutes=8),
    )
    boundary = MarkerBoundary(CTX, UID[0], "chat", 7, SHA, 4, NOW - timedelta(minutes=11), SHA)
    cluster = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": {
            "namespace": "test",
            "name": "restored",
            "uid": UID[2],
            "generation": 1,
            "creationTimestamp": "2026-11-04T00:53:00Z",
        },
        "spec": {
            "instances": 1,
            "enableSuperuserAccess": False,
            "bootstrap": {
                "recovery": {
                    "source": "source",
                    "recoveryTarget": {"backupID": backup.backup_id, "targetImmediate": True},
                }
            },
            "externalClusters": [
                {
                    "name": "source",
                    "plugin": {
                        "name": "barman-cloud.cloudnative-pg.io",
                        "enabled": True,
                        "isWALArchiver": False,
                        "parameters": {"barmanObjectName": "reader", "serverName": "original"},
                    },
                }
            ],
        },
        "status": {
            "phase": "Cluster in healthy state",
            "readyInstances": 1,
            "instances": 1,
            "currentPrimary": "restored-1",
            "targetPrimary": "restored-1",
            "instanceNames": ["restored-1"],
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": "2026-11-04T00:55:00Z"},
                {"type": "Initialized", "status": "True"},
            ],
        },
    }
    store = {
        "apiVersion": "barmancloud.cnpg.io/v1",
        "kind": "ObjectStore",
        "metadata": {"namespace": "test", "name": "reader", "uid": UID[3], "generation": 1},
        "spec": {"configuration": {"destinationPath": "s3://test/archive/"}},
    }
    docs = {"clusters.postgresql.cnpg.io": cluster, "objectstores.barmancloud.cnpg.io": store}
    binding = RestoreRequestBinding(
        CTX,
        backup,
        "test",
        "original",
        "restored",
        UID[2],
        resource_fingerprint(
            cluster, kind="Cluster", namespace="test", name="restored", uid=UID[2]
        ),
        "reader",
        UID[3],
        resource_fingerprint(
            store, kind="ObjectStore", namespace="test", name="reader", uid=UID[3]
        ),
        MARKER,
        NOW - timedelta(minutes=7),
    )
    calls = []
    evidence = []

    @contextmanager
    def connect(context, uid, primary, host, database):
        calls.append((context, uid, primary, host, database))
        yield SimpleNamespace(info=SimpleNamespace(host=host, dbname=database))

    def read(connection, **kwargs):
        calls.append(kwargs)
        return MarkerReadback(7, SHA, 4, NOW)

    monkeypatch.setattr("bravo_controls.cnpg_restore_readback.read_demo_chat_marker", read)

    def make(**kwargs):
        return CNPGRestoreReader(
            binding=binding,
            get_resource=lambda resource, *_: deepcopy(docs[resource]),
            connection_factory=connect,
            evidence_sink=evidence.append,
            clock=lambda: NOW,
            **kwargs,
        )

    return docs, binding, boundary, calls, evidence, make


def test_exact_restore_and_query_binding(monkeypatch):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    result = make()(CTX, b.backup, boundary)
    assert result.target_uid == b.target_uid and result.source_uid != result.target_uid
    assert result.marker_id == 7 and result.row_count == 4
    assert calls[0] == (CTX, b.target_uid, "restored-1", "restored-rw.test.svc", "chat")
    assert calls[1]["idempotency_key"] == MARKER
    assert b.target_uid.encode() not in evidence[0] and b"s3://" not in evidence[0]


@pytest.mark.parametrize(
    "path,value",
    [
        (("metadata", "uid"), UID[0]),
        (("metadata", "generation"), 2),
        (("metadata", "deletionTimestamp"), "2026-11-04T01:00:00Z"),
        (("spec", "bootstrap", "recovery", "recoveryTarget", "backupID"), "latest"),
        (("spec", "instances"), 2),
        (("spec", "enableSuperuserAccess"), True),
        (("status", "phase"), "Setting up primary"),
        (("status", "readyInstances"), 0),
        (("status", "readyInstances"), True),
        (("status", "instances"), True),
        (("status", "currentPrimary"), "other"),
        (("status", "targetPrimary"), "other"),
        (("status", "instanceNames"), ["restored-1", "other"]),
        (("status", "conditions"), []),
        (("metadata", "creationTimestamp"), "2026-11-04T00:50:00Z"),
        (("metadata", "creationTimestamp"), "2026-11-04T02:00:00Z"),
    ],
)
def test_wrong_or_unready_restore_never_queries(monkeypatch, path, value):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    cursor = docs["clusters.postgresql.cnpg.io"]
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ValueError):
        make()(CTX, b.backup, boundary)
    assert not calls and not evidence


def test_archive_changes_rejected_before_query(monkeypatch):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    docs["objectstores.barmancloud.cnpg.io"]["spec"]["configuration"]["destinationPath"] = (
        "s3://other/"
    )
    with pytest.raises(ValueError):
        make()(CTX, b.backup, boundary)
    assert not calls and not evidence


@pytest.mark.parametrize("change", ["replace", "not-ready", "primary", "store"])
def test_change_during_query_does_not_emit_receipt(monkeypatch, change):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)

    def read(*args, **kwargs):
        c = docs["clusters.postgresql.cnpg.io"]
        if change == "replace":
            c["metadata"]["uid"] = UID[0]
        if change == "not-ready":
            c["status"]["conditions"][0]["status"] = "False"
        if change == "primary":
            c["status"].update(
                currentPrimary="restored-2",
                targetPrimary="restored-2",
                instanceNames=["restored-2"],
            )
        if change == "store":
            docs["objectstores.barmancloud.cnpg.io"]["metadata"]["generation"] = 2
        return MarkerReadback(7, SHA, 4, NOW)

    monkeypatch.setattr("bravo_controls.cnpg_restore_readback.read_demo_chat_marker", read)
    with pytest.raises(ValueError):
        make()(CTX, b.backup, boundary)
    assert not evidence


@pytest.mark.parametrize("change", ["context", "backup", "boundary"])
def test_foreign_inputs_rejected_before_io(monkeypatch, change):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    context = CTX
    backup = b.backup
    if change == "context":
        context = replace(CTX, app="other")
    if change == "backup":
        backup = replace(backup, backup_uid=UID[3])
    if change == "boundary":
        boundary = replace(boundary, source_uid=UID[3])
    with pytest.raises(ValueError):
        make()(context, backup, boundary)
    assert not calls and not evidence


def test_wrong_connection_endpoint_rejected(monkeypatch):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    reader = make()

    @contextmanager
    def connect(*args):
        yield SimpleNamespace(info=SimpleNamespace(host="original-rw.test.svc", dbname="chat"))

    reader.connect = connect
    with pytest.raises(ValueError):
        reader(CTX, b.backup, boundary)
    assert not evidence


@pytest.mark.parametrize("error", [TimeoutError, PermissionError, ConnectionError])
def test_client_errors_never_create_receipt(monkeypatch, error):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)
    reader = make()

    def get(*args):
        raise error("redacted")

    reader.get = get
    with pytest.raises(error):
        reader(CTX, b.backup, boundary)
    assert not evidence


def test_elapsed_time_and_missing_marker(monkeypatch):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)

    def read(*args, **kwargs):
        return MarkerReadback(None, None, 4, NOW)

    monkeypatch.setattr("bravo_controls.cnpg_restore_readback.read_demo_chat_marker", read)
    result = make()(CTX, b.backup, boundary)
    assert result.marker_id is None  # RestoreProbe will FAIL; reader must not invent it.
    evidence.clear()
    reader = make()
    ticks = iter([NOW, NOW + timedelta(seconds=31), NOW + timedelta(seconds=32)])
    reader.clock = lambda: next(ticks)
    with pytest.raises(ValueError):
        reader(CTX, b.backup, boundary)
    assert not evidence


def test_storage_failure_and_invalid_binding(monkeypatch):
    docs, b, boundary, calls, evidence, make = setup(monkeypatch)

    def fail(*args):
        raise OSError("storage unavailable")

    reader = make()
    reader.sink = fail
    with pytest.raises(OSError):
        reader(CTX, b.backup, boundary)
    with pytest.raises(ValueError):
        replace(b, target_uid=b.backup.source_uid)
    with pytest.raises(ValueError):
        replace(b, marker_uuid="not-a-uuid")
    reader = make()
    reader.binding = replace(b, requested_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError):
        reader(CTX, b.backup, boundary)
