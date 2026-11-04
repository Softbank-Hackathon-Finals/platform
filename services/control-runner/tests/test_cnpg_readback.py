from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.cnpg_readback import CNPGBackupReader, RequestBinding, resource_fingerprint
from bravo_controls.runner import Environment, RunContext

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
CTX = RunContext("restore-a", "chat", Environment("target", "target", "k8s:registered"))
PLUGIN = "barman-cloud.cloudnative-pg.io"
UIDS = {
    k: f"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee{i}"
    for i, k in enumerate(("Cluster", "ObjectStore", "Backup"))
}


def fixture():
    docs = {}
    for kind, name in [("Cluster", "source"), ("ObjectStore", "archive"), ("Backup", "backup")]:
        docs[kind] = {
            "apiVersion": "barmancloud.cnpg.io/v1"
            if kind == "ObjectStore"
            else "postgresql.cnpg.io/v1",
            "kind": kind,
            "metadata": {
                "namespace": "test",
                "name": name,
                "uid": UIDS[kind],
                "generation": 1,
                "creationTimestamp": "2026-11-03T11:58:00Z",
            },
            "spec": {},
        }
    docs["Cluster"]["spec"] = {
        "plugins": [
            {"name": PLUGIN, "isWALArchiver": True, "parameters": {"barmanObjectName": "archive"}}
        ]
    }
    docs["ObjectStore"]["spec"] = {
        "configuration": {
            "destinationPath": "s3://test/archive/",
            "s3Credentials": {"inheritFromIAMRole": True},
        }
    }
    docs["Backup"]["spec"] = {
        "cluster": {"name": "source"},
        "method": "plugin",
        "pluginConfiguration": {"name": PLUGIN},
    }
    docs["Backup"]["status"] = {
        "phase": "completed",
        "method": "plugin",
        "backupId": "20261103T115800",
        "startedAt": "2026-11-03T11:58:00Z",
        "stoppedAt": "2026-11-03T11:59:00Z",
        "pluginMetadata": {"clusterUID": UIDS["Cluster"], "name": PLUGIN, "pluginName": PLUGIN},
    }

    def fingerprint(k, n):
        return resource_fingerprint(docs[k], kind=k, namespace="test", name=n, uid=UIDS[k])

    binding = RequestBinding(
        CTX,
        "test",
        "source",
        UIDS["Cluster"],
        fingerprint("Cluster", "source"),
        "archive",
        UIDS["ObjectStore"],
        fingerprint("ObjectStore", "archive"),
        "backup",
        UIDS["Backup"],
        NOW - timedelta(minutes=2),
    )
    return docs, binding


def make_reader(docs, binding, *, get=None, sink=None, clock=lambda: NOW):
    evidence = []
    calls = []

    def read(resource, namespace, name):
        calls.append((resource, namespace, name))
        return docs[
            {
                "clusters.postgresql.cnpg.io": "Cluster",
                "objectstores.barmancloud.cnpg.io": "ObjectStore",
                "backups.postgresql.cnpg.io": "Backup",
            }[resource]
        ]

    return (
        CNPGBackupReader(
            binding=binding,
            get_resource=get or read,
            evidence_sink=sink or evidence.append,
            clock=clock,
        ),
        evidence,
        calls,
    )


def test_authenticated_completed_backup_is_bound_and_not_data_verification():
    docs, b = fixture()
    p, evidence, calls = make_reader(docs, b)
    got = p(CTX)
    assert got.source_uid == b.source_uid and got.backup_uid == b.backup_uid
    assert got.archive_binding_sha256 == b.archive_binding_sha256
    assert len(calls) == 6 and {c[2] for c in calls} == {"source", "archive", "backup"}
    assert b"controller-completed-backup-not-restored-data" in evidence[0]
    assert b"s3://" not in evidence[0] and b.source_uid.encode() not in evidence[0]


@pytest.mark.parametrize(
    "kind,path,value",
    [
        ("Cluster", ("metadata", "uid"), UIDS["Backup"]),
        ("ObjectStore", ("metadata", "uid"), UIDS["Backup"]),
        ("Backup", ("metadata", "uid"), UIDS["Cluster"]),
        ("Backup", ("metadata", "namespace"), "other"),
        ("Backup", ("metadata", "name"), "other"),
        ("Backup", ("metadata", "deletionTimestamp"), "2026-11-03T12:00:00Z"),
        ("Cluster", ("metadata", "generation"), 2),
        ("ObjectStore", ("metadata", "generation"), 2),
        ("ObjectStore", ("spec", "configuration", "destinationPath"), "s3://other/archive/"),
        ("Backup", ("spec", "cluster", "name"), "other"),
        ("Backup", ("status", "phase"), "failed"),
        ("Backup", ("status", "phase"), "running"),
        ("Backup", ("status", "error"), "failure"),
        ("Backup", ("status", "method"), "barmanObjectStore"),
        ("Backup", ("status", "pluginMetadata", "clusterUID"), UIDS["Backup"]),
        ("Backup", ("status", "pluginMetadata", "pluginName"), "other"),
        ("Backup", ("status", "backupId"), "latest"),
        ("Backup", ("status", "backupId"), "20260231T120000"),
        ("Backup", ("status", "stoppedAt"), "2026-11-04T12:00:00Z"),
        ("Backup", ("status", "startedAt"), "2026-11-03T11:57:00Z"),
        ("Backup", ("status", "stoppedAt"), None),
        ("Backup", ("metadata", "generation"), True),
        ("Backup", ("metadata", "generation"), 2),
        ("Backup", ("kind",), "Cluster"),
        ("Backup", ("apiVersion",), "untrusted/v1"),
    ],
)
def test_untrusted_or_inconsistent_status_is_rejected(kind, path, value):
    docs, b = fixture()
    cursor = docs[kind]
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    p, evidence, _ = make_reader(docs, b)
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


@pytest.mark.parametrize(
    "change",
    [
        {"requested_at": NOW + timedelta(seconds=1)},
        {"requested_at": NOW - timedelta(hours=25)},
        {"context": replace(CTX, app="another-app")},
    ],
)
def test_request_freshness_and_ownership(change):
    docs, b = fixture()
    p, evidence, calls = make_reader(docs, replace(b, **change))
    with pytest.raises(ValueError):
        p(CTX)
    assert not calls and not evidence


@pytest.mark.parametrize("kind", ["Cluster", "ObjectStore", "Backup"])
def test_resource_replacement_between_reads_is_rejected(kind):
    docs, b = fixture()
    count = 0

    def get(resource, namespace, name):
        nonlocal count
        count += 1
        k = {"source": "Cluster", "archive": "ObjectStore", "backup": "Backup"}[name]
        doc = deepcopy(docs[k])
        if count > 3 and k == kind:
            doc["metadata"]["uid"] = "ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        return doc

    p, evidence, _ = make_reader(docs, b, get=get)
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


def test_second_read_changed_status_rejected():
    docs, b = fixture()
    count = 0

    def get(resource, namespace, name):
        nonlocal count
        count += 1
        k = {"source": "Cluster", "archive": "ObjectStore", "backup": "Backup"}[name]
        doc = deepcopy(docs[k])
        if count == 6:
            doc["status"]["backupId"] = "20261103T115801"
        return doc

    p, evidence, _ = make_reader(docs, b, get=get)
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


@pytest.mark.parametrize("error", [TimeoutError(), PermissionError(), ConnectionError()])
def test_observation_errors_do_not_create_receipt(error):
    docs, b = fixture()

    def get(*args):
        raise error

    p, evidence, _ = make_reader(docs, b, get=get)
    with pytest.raises(type(error)):
        p(CTX)
    assert not evidence


def test_evidence_storage_failure_prevents_receipt_return():
    docs, b = fixture()

    def sink(_):
        raise OSError("unavailable")

    p, _, _ = make_reader(docs, b, sink=sink)
    with pytest.raises(OSError):
        p(CTX)


@pytest.mark.parametrize("seconds", [-1, 31])
def test_slow_or_backwards_readback_rejected(seconds):
    docs, b = fixture()
    times = iter([NOW, NOW + timedelta(seconds=seconds)])
    p, evidence, _ = make_reader(docs, b, clock=lambda: next(times))
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


def test_large_response_rejected():
    docs, b = fixture()
    docs["Backup"]["status"]["unused"] = "x" * 262144
    p, evidence, _ = make_reader(docs, b)
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


def test_unrelated_status_reconciliation_is_allowed():
    docs, b = fixture()
    docs["Cluster"]["status"] = {"phase": "Cluster in healthy state"}
    docs["Cluster"]["metadata"]["resourceVersion"] = "444"
    p, _, _ = make_reader(docs, b)
    assert p(CTX).backup_id == "20261103T115800"


@pytest.mark.parametrize(
    "plugins",
    [
        [],
        [
            {
                "name": PLUGIN,
                "isWALArchiver": True,
                "enabled": False,
                "parameters": {"barmanObjectName": "archive"},
            }
        ],
        [{"name": PLUGIN, "isWALArchiver": False, "parameters": {"barmanObjectName": "archive"}}],
        [{"name": PLUGIN, "isWALArchiver": True, "parameters": {"barmanObjectName": "other"}}],
        [{"name": PLUGIN, "isWALArchiver": True, "parameters": {"barmanObjectName": "archive"}}]
        * 2,
    ],
)
def test_matching_fingerprint_does_not_substitute_for_plugin_binding(plugins):
    docs, b = fixture()
    docs["Cluster"]["spec"]["plugins"] = plugins
    fingerprint = resource_fingerprint(
        docs["Cluster"], kind="Cluster", namespace="test", name="source", uid=b.source_uid
    )
    p, evidence, _ = make_reader(docs, replace(b, source_sha256=fingerprint))
    with pytest.raises(ValueError):
        p(CTX)
    assert not evidence


@pytest.mark.parametrize(
    "change",
    [
        {"source_uid": "not-a-uid"},
        {"backup_name": "../another"},
        {"namespace": "a/b"},
        {"store_sha256": "unverified"},
        {"requested_at": datetime(2026, 11, 3)},
        {"context": {}},
    ],
)
def test_invalid_binding_rejected(change):
    _, b = fixture()
    with pytest.raises(ValueError):
        replace(b, **change)
