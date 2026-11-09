"""Qualification failures stay separate from the T14/19 security verdict."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "restore_failure", Path(__file__).parents[1] / "qualification/verify_restore_failure.py"
)
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)
UID = "c2186bd3-5833-421b-a16b-08b83bc9721a"
BACKUP = "19990101T000000"
WAL = "000000010000000000000006"


def error(case):
    event = {"level": "error", "logger": "barman", "command": "barman-cloud-backup-list"}
    if case == "missing-backup":
        event["error"] = "rpc error: code = Unknown desc = no backup found with ID " + BACKUP
    elif case == "wrong-ca":
        event["stderr"] = "SSL validation failed: [SSL: CERTIFICATE_VERIFY_FAILED]"
    elif case == "missing-wal":
        event = {
            "level": "error",
            "msg": "Error while restoring a backup",
            "error": "rpc error: code = Unknown desc = encountered an error while checking "
            "the presence of first needed WAL in the archive: object storage or file "
            "not found " + WAL + ": WAL not found",
        }
    else:
        event["stderr"] = "An error occurred (InvalidAccessKeyId) when calling ListObjectsV2"
    return event


def encoded(event):
    return json.dumps(event).encode()


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing-backup", "requested_backup_missing"),
        ("wrong-ca", "certificate_verification_failed"),
        ("wrong-credentials", "storage_access_rejected"),
        ("missing-wal", "required_initial_wal_missing"),
    ],
)
def test_specific_actual_error_formats(case, reason):
    assert (
        q.failure_reason(
            case, BACKUP, encoded(error(case)), wal_segment=WAL if case == "missing-wal" else None
        )
        == reason
    )


@pytest.mark.parametrize("case", sorted(q.CASES))
@pytest.mark.parametrize("change", ["info", "message-only", "duplicate-key", "generic"])
def test_untrusted_or_ambiguous_log_is_not_proof(case, change):
    event = error(case)
    if change == "info":
        event["level"] = "info"
    elif change == "message-only":
        event = {"level": "error", "msg": json.dumps(event)}
    elif change == "generic":
        event = {"level": "error", "error": "exit status 4; restore failed"}
    raw = encoded(event)
    if change == "duplicate-key":
        raw = raw[:-1] + b', "level": "error"}'
    assert (
        q.failure_reason(case, BACKUP, raw, wal_segment=WAL if case == "missing-wal" else None)
        is None
    )


def test_wrong_backup_id_or_longer_prefix_is_not_matching():
    for value in ["20000101T000000", BACKUP + "9"]:
        assert (
            q.failure_reason(
                "missing-backup",
                BACKUP,
                encoded({"level": "error", "error": "no backup found with ID " + value}),
            )
            is None
        )


@pytest.mark.parametrize("change", ["command", "logger"])
def test_ca_and_credentials_require_barman_command(change):
    event = error("wrong-ca")
    event[change] = "unrelated"
    assert q.failure_reason("wrong-ca", BACKUP, encoded(event)) is None


def test_log_limit_and_unknown_case_rejected():
    with pytest.raises(ValueError):
        q.failure_reason("wrong-ca", BACKUP, b"x" * (262144 + 1))
    with pytest.raises(ValueError):
        q.failure_reason("timeout", BACKUP, b"")


class Client:
    def __init__(self):
        self.cluster = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": "trial", "uid": UID},
            "spec": {"bootstrap": {"recovery": {"recoveryTarget": {"backupID": BACKUP}}}},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        }
        self.pod = {
            "metadata": {
                "name": "trial-pod",
                "uid": "pod-uid",
                "ownerReferences": [
                    {"kind": "Job", "name": "restore", "uid": "job-uid", "controller": True}
                ],
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "full-recovery",
                        "state": {"terminated": {"reason": "Error", "exitCode": 1}},
                    }
                ]
            },
        }
        self.job = {
            "metadata": {
                "uid": "job-uid",
                "ownerReferences": [
                    {"kind": "Cluster", "name": "trial", "uid": UID, "controller": True}
                ],
            }
        }
        self.log = encoded(error("missing-backup"))
        self.after_cluster = None
        self.after_pod = None
        self.reads = 0

    def get(self, kind, name):
        if kind == "cluster":
            self.reads += 1
            return self.after_cluster if self.reads > 1 and self.after_cluster else self.cluster
        if kind == "pod":
            return self.after_pod or self.pod
        return self.job

    def raw(self, *args, **kwargs):
        return self.log if args[0] == "logs" else encoded({"items": [self.pod]})


def inspect(client):
    return q.inspect(
        client,
        case="missing-backup",
        name="trial",
        uid=UID,
        expected_spec=q.spec_digest(Client().cluster),
        backup_id=BACKUP,
    )


def test_bound_failure_succeeds_without_raw_logs_or_identifiers():
    value = inspect(Client())
    assert value["qualified_failure"] is True
    assert value["cluster_ready"] is False
    assert "not DR-01 PASS" in value["scope"]
    assert value["evidence"]["log_sha256"] == q.digest(Client().log)
    assert UID not in json.dumps(value)
    assert "no backup found" not in json.dumps(value)


@pytest.mark.parametrize(
    "fault",
    [
        "cluster-uid",
        "spec",
        "ready",
        "deletion",
        "job-uid",
        "job-owner",
        "exit-zero",
        "oom",
        "init-only",
        "log",
    ],
)
def test_unbound_or_unproven_failure_rejected(fault):
    client = Client()
    if fault == "cluster-uid":
        client.cluster["metadata"]["uid"] = "other"
    elif fault == "spec":
        client.cluster["spec"]["instances"] = 2
    elif fault == "ready":
        client.cluster["status"]["conditions"][0]["status"] = "True"
    elif fault == "deletion":
        client.cluster["metadata"]["deletionTimestamp"] = "2026-11-03T00:00:00Z"
    elif fault == "job-uid":
        client.job["metadata"]["uid"] = "other"
    elif fault == "job-owner":
        client.job["metadata"]["ownerReferences"][0]["uid"] = "other"
    elif fault == "exit-zero":
        client.pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 0
    elif fault == "oom":
        client.pod["status"]["containerStatuses"][0]["state"]["terminated"]["reason"] = "OOMKilled"
    elif fault == "init-only":
        client.pod["status"]["initContainerStatuses"] = client.pod["status"].pop(
            "containerStatuses"
        )
    else:
        client.log = b'{"level":"error","error":"timeout"}'
    with pytest.raises(ValueError):
        inspect(client)


@pytest.mark.parametrize("fault", ["replaced-pod", "ready-after", "changed-spec-after"])
def test_reread_detects_changes(fault):
    client = Client()
    if fault == "replaced-pod":
        client.after_pod = copy.deepcopy(client.pod)
        client.after_pod["metadata"]["uid"] = "other"
    else:
        client.after_cluster = copy.deepcopy(client.cluster)
        if fault == "ready-after":
            client.after_cluster["status"]["conditions"][0]["status"] = "True"
        else:
            client.after_cluster["spec"]["instances"] = 2
    with pytest.raises(ValueError):
        inspect(client)


def test_missing_wal_is_bound_to_reviewed_backup_segment():
    client = Client()
    client.log = encoded(error("missing-wal"))
    result = q.inspect(
        client,
        case="missing-wal",
        name="trial",
        uid=UID,
        expected_spec=q.spec_digest(client.cluster),
        backup_id=BACKUP,
        wal_segment=WAL,
    )
    assert result["qualified_failure"] is True
    assert result["evidence"]["reason"] == "required_initial_wal_missing"
    assert "not DR-01 PASS" in result["scope"]
    for incorrect in (None, "", "../wal", WAL.lower().replace("0", "g", 1)):
        with pytest.raises(ValueError):
            q.failure_reason("missing-wal", BACKUP, client.log, wal_segment=incorrect)
    assert q.failure_reason("missing-wal", BACKUP, client.log, wal_segment="0" * 24) is None


@pytest.mark.parametrize(
    "fault", ["future-wal", "unrelated-message", "partial", "suffix", "timeout"]
)
def test_other_archive_errors_do_not_qualify_initial_wal_loss(fault):
    event = error("missing-wal")
    if fault == "future-wal":
        event["error"] = event["error"].replace("first needed WAL", "next WAL")
    elif fault == "unrelated-message":
        event["msg"] = "application message"
    elif fault == "partial":
        event["error"] = "WAL not found"
    elif fault == "suffix":
        event["error"] += "; unrelated"
    else:
        event["error"] = "connection timeout"
    assert q.failure_reason("missing-wal", BACKUP, encoded(event), wal_segment=WAL) is None
