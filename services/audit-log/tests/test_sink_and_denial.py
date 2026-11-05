from datetime import UTC, datetime, timedelta

import pytest

from bravo_audit.denial import probe_denial
from bravo_audit.sink import FileSink, ObjectLockSink, UnverifiedWrite

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


class Denied(Exception):
    def __init__(self, code="AccessDenied", status=403):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class LockedStore:
    """Behaves like a versioned Object Lock bucket seen by a write-only identity."""

    def __init__(self, *, versioning=True, lock=True, allow_delete=False, allow_policy=False):
        self.objects, self.calls = {}, []
        self.versioning, self.lock, self.allow_delete, self.allow_policy = versioning, lock, allow_delete, allow_policy

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.objects:
            raise Denied("PreconditionFailed", 412)
        version = f"v{len(self.objects) + 1}" if self.versioning else "null"
        self.objects[kw["Key"]] = {"version": version, "mode": kw.get("ObjectLockMode") if self.lock else None,
                                   "until": kw.get("ObjectLockRetainUntilDate") if self.lock else None}
        return {"VersionId": version}

    def head_object(self, **kw):
        o = self.objects[kw["Key"]]
        return {"ObjectLockMode": o["mode"], "ObjectLockRetainUntilDate": o["until"]}

    def delete_object(self, **kw):
        self.calls.append(("delete_object", kw))
        if self.allow_delete:
            self.objects.pop(kw["Key"], None)
            return {}
        raise Denied()

    def put_object_retention(self, **kw):
        raise Denied("InvalidRequest", 400)

    def put_bucket_policy(self, **kw):
        if self.allow_policy:
            return {}
        raise Denied()

    def put_object_lock_configuration(self, **kw):
        raise Denied()

    def get_object_retention(self, **kw):
        o = self.objects[kw["Key"]]
        if o["mode"] is None:
            raise Denied("ObjectLockConfigurationNotFoundError", 404)
        return {"Retention": {"Mode": o["mode"], "RetainUntilDate": o["until"]}}


def test_file_sink_never_overwrites(tmp_path, ledger):
    sink = FileSink(tmp_path)
    r = ledger.append("gate", "gate.completed", {"decision": "PASS"})
    receipt = sink.put(r)
    assert receipt.key == "audit/plan-demo/run-demo-1/00000001.json" and receipt.version_id == "file"
    assert (tmp_path / receipt.key).stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        sink.put(r)


def test_object_lock_sink_requests_compliance_retention_and_no_overwrite(ledger):
    store = LockedStore()
    sink = ObjectLockSink(store, "bravo-audit", retention_days=7, now=lambda: NOW)
    r = ledger.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": "bucket_delete"})
    receipt = sink.put(r)
    _, kw = store.calls[0]
    assert kw["IfNoneMatch"] == "*" and kw["ObjectLockMode"] == "COMPLIANCE"
    assert kw["ObjectLockRetainUntilDate"] == NOW + timedelta(days=7) and kw["ChecksumSHA256"]
    assert kw["Key"] == "audit/plan-demo/run-demo-1/00000001.json"
    assert receipt.version_id == "v1" and receipt.retention_mode == "COMPLIANCE" and receipt.retain_until == "2026-11-12T10:00:00Z"
    with pytest.raises(Denied):  # same key again → store precondition, never a second version
        sink.put(r)


@pytest.mark.parametrize("store", [LockedStore(versioning=False), LockedStore(lock=False)])
def test_object_lock_sink_refuses_to_report_an_unretained_write(ledger, store):
    sink = ObjectLockSink(store, "bravo-audit", now=lambda: NOW)
    r = ledger.append("gate", "gate.completed", {"decision": "PASS"})
    with pytest.raises(UnverifiedWrite):
        sink.put(r)


def test_denial_probe_passes_only_when_every_attempt_is_refused(ledger):
    store = LockedStore()
    r = ledger.append("gate", "gate.completed", {"decision": "PASS"})
    receipt = ObjectLockSink(store, "bravo-audit", now=lambda: NOW).put(r)
    result = probe_denial(store, "bravo-audit", receipt.key, receipt.version_id, now=lambda: NOW)
    assert result["pass"] is True and result["retention"] == "COMPLIANCE_ACTIVE"
    assert result["checks"]["overwrite_precondition_failed"] == "PRECONDITION_FAILED"
    assert set(result["checks"].values()) == {"DENIED", "PRECONDITION_FAILED"}
    assert "bravo-audit" not in str(result)  # bucket name is not echoed
    assert receipt.key in store.objects  # probing did not remove the record


@pytest.mark.parametrize("kw, failing", [
    ({"allow_delete": True}, "delete_current_denied"),
    ({"allow_policy": True}, "bucket_policy_change_denied"),
])
def test_denial_probe_fails_when_the_store_allows_tampering(ledger, kw, failing):
    store = LockedStore(**kw)
    r = ledger.append("gate", "gate.completed", {"decision": "PASS"})
    receipt = ObjectLockSink(store, "bravo-audit", now=lambda: NOW).put(r)
    result = probe_denial(store, "bravo-audit", receipt.key, receipt.version_id, now=lambda: NOW)
    assert result["pass"] is False and result["checks"][failing] == "ALLOWED"
