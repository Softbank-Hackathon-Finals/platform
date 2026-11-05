import io
import json
from datetime import UTC, datetime, timedelta

import botocore.session
import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber

from bravo_controls.audit_probes import AuditSearchProbe, CanaryReceipt
from bravo_controls.audit_store import ObjectStoreAuditSearch, _minutes
from bravo_controls.otlp_audit import AuditOrigin
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

START = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
CTX = RunContext("run-1", "demo-chat", Environment("target", "target", "onprem:rke2/test"))
MARKER = "bravo_audit_" + "b" * 32
BUCKET, PREFIX = "bravo-audit", "audit/onprem"
ORIGIN = AuditOrigin(
    "target",
    "sha256:" + "c" * 64,
    "22222222-2222-4222-8222-222222222222",
    "11111111-1111-4111-8111-111111111111",
    "target-dev",
    "target-db-1",
)
UUID7 = "01a1a801-c758-7182-83a6-5f55274e5521"
VERSION = "671b122adb390a798abee65cc0967cf6"


def receipt():
    return CanaryReceipt(
        CTX, "AUD-01.1", MARKER, "postgres", "chat", "target-dev", "bravo_test",
        START, START + timedelta(seconds=1), START + timedelta(minutes=10), frozenset({"login"}),
    )


def value(item):
    if isinstance(item, dict):
        return {"kvlistValue": {"values": [{"key": k, "value": value(v)} for k, v in item.items()]}}
    return {"stringValue": item}


def login(marker=MARKER):
    return {
        "logger": "postgres",
        "record": {
            "user_name": "postgres",
            "database_name": "chat",
            "application_name": marker,
            "error_severity": "LOG",
            "message": "connection authorized: user=postgres database=chat application_name=" + marker,
        },
    }


def export(*records, origin=ORIGIN):
    attrs = [{"key": k, "value": value(v)} for k, v in origin.attributes("cnpg").items()]
    rows = [{"timeUnixNano": str(int(at.timestamp()) * 10**9), "body": value(body)} for at, body in records]
    document = {"resourceLogs": [{"resource": {"attributes": attrs}, "scopeLogs": [{"scope": {}, "logRecords": rows}]}]}
    return json.dumps(document).encode()


def client():
    return botocore.session.get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://audit-store:8333",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(
            connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 1},
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )


def key(minute, uuid=UUID7):
    return f"{PREFIX}/{minute:%Y/%m/%d/%H/%M}/logs_{uuid}.json"


def version(k, modified, size, version_id=VERSION):
    return {"Key": k, "VersionId": version_id, "IsLatest": True, "LastModified": modified, "Size": size}


def listing(stub, through, objects=None, extra=None):
    """Stub every minute partition the search must read; objects maps minute → listing rows."""
    for minute in _minutes(START - timedelta(minutes=1), through + timedelta(minutes=1)):
        partition = f"{PREFIX}/{minute:%Y/%m/%d/%H/%M}/"
        page = {"Versions": (objects or {}).get(minute, []), "IsTruncated": False,
                "ResponseMetadata": {"HTTPStatusCode": 200}, **((extra or {}).get(minute, {}))}
        stub.add_response("list_object_versions", page, {"Bucket": BUCKET, "Prefix": partition, "MaxKeys": 1000})


RETAINED = START + timedelta(days=1)


def fetch(stub, k, data, modified, lock="COMPLIANCE", version_id=VERSION, retain_until=RETAINED):
    body = {"Body": StreamingBody(io.BytesIO(data), len(data)), "VersionId": version_id, "LastModified": modified,
            "ObjectLockMode": lock, "ContentLength": len(data), "ResponseMetadata": {"HTTPStatusCode": 200}}
    if retain_until is not None:
        body["ObjectLockRetainUntilDate"] = retain_until
    stub.add_response("get_object", body, {"Bucket": BUCKET, "Key": k, "VersionId": version_id})


def search(c, now=START + timedelta(minutes=10)):
    return ObjectStoreAuditSearch(c, bucket=BUCKET, prefix=PREFIX, region="us-east-1", origin=ORIGIN, clock=lambda: now)


MINUTE = START + timedelta(minutes=1)
MODIFIED = START + timedelta(seconds=65)


def test_retained_canary_is_found_with_store_receipt_time():
    c, through = client(), START + timedelta(minutes=10)
    data = export((START + timedelta(seconds=2), login()), (START + timedelta(seconds=3), login("other")))
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
        fetch(stub, key(MINUTE), data, MODIFIED)
        batch = search(c)(receipt(), through)
        stub.assert_no_pending_responses()
    assert batch.complete and batch.source_binding == "s3://bravo-audit/audit/onprem/"
    assert batch.streams == ORIGIN.streams
    (event,) = batch.events  # the record without the marker is not returned
    assert event.ingested_at == MODIFIED + timedelta(seconds=1)
    assert MARKER in event.message


def test_probe_judges_store_search_as_present():
    c, now = client(), START + timedelta(minutes=10)
    data = export((START + timedelta(seconds=2), login()))
    payloads = []
    with Stubber(c) as stub:
        listing(stub, now, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
        fetch(stub, key(MINUTE), data, MODIFIED)
        adapter = search(c, now)
        probe = AuditSearchProbe(
            receipt_provider=lambda *_: receipt(), search=adapter, source_binding=adapter.source_binding,
            streams=ORIGIN.streams, evidence_sink=payloads.append, clock=lambda: now,
        )
        observation = probe(CTX, Spec("AUD-01.1", "AUD-01", "1", "present", "not_applicable"))
    assert observation.observed == "present" and observation.unknown_reason is None


def test_several_versions_of_one_key_is_tamper_evidence():
    c, through = client(), START + timedelta(minutes=10)
    rows = [version(key(MINUTE), MODIFIED, 10), version(key(MINUTE), MODIFIED, 10, "1" * 32)]
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: rows})
        with pytest.raises(ValueError, match="tamper evidence: object has several versions"):
            search(c)(receipt(), through)


def test_delete_marker_hidden_from_plain_listing_is_tamper_evidence():
    c, through = client(), START + timedelta(minutes=10)
    marker = {"DeleteMarkers": [{"Key": key(MINUTE), "VersionId": "2" * 32, "IsLatest": True, "LastModified": MODIFIED}]}
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, 10)]}, {MINUTE: marker})
        with pytest.raises(ValueError, match="tamper evidence: delete marker"):
            search(c)(receipt(), through)


def test_unretained_or_changed_object_is_refused():
    for lock, version_id in (("GOVERNANCE", VERSION), (None, VERSION)):
        c, through = client(), START + timedelta(minutes=10)
        data = export((START + timedelta(seconds=2), login()))
        with Stubber(c) as stub:
            listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
            body = {"Body": StreamingBody(io.BytesIO(data), len(data)), "VersionId": version_id,
                    "LastModified": MODIFIED, "ContentLength": len(data), "ObjectLockRetainUntilDate": RETAINED,
                    "ResponseMetadata": {"HTTPStatusCode": 200}}
            if lock:
                body["ObjectLockMode"] = lock
            stub.add_response("get_object", body, {"Bucket": BUCKET, "Key": key(MINUTE), "VersionId": VERSION})
            with pytest.raises(ValueError, match="not retained"):
                search(c)(receipt(), through)


def test_foreign_origin_or_unexpected_key_is_refused():
    c, through = client(), START + timedelta(minutes=10)
    other = AuditOrigin("target", "sha256:" + "d" * 64, ORIGIN.node_uid, ORIGIN.pod_uid, "target-dev", "target-db-1")
    data = export((START + timedelta(seconds=2), login()), origin=other)
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
        fetch(stub, key(MINUTE), data, MODIFIED)
        with pytest.raises(ValueError, match="origin differs"):
            search(c)(receipt(), through)
    c = client()
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(f"{PREFIX}/{MINUTE:%Y/%m/%d/%H/%M}/notes.txt", MODIFIED, 10)]})
        with pytest.raises(ValueError, match="unexpected object"):
            search(c)(receipt(), through)


def test_repeated_pagination_or_denied_listing_is_not_a_search():
    c, through = client(), START + timedelta(minutes=10)
    first = START - timedelta(minutes=1)
    with Stubber(c) as stub:
        page = {"Versions": [], "IsTruncated": True, "NextKeyMarker": "k", "NextVersionIdMarker": "v",
                "ResponseMetadata": {"HTTPStatusCode": 200}}
        partition = f"{PREFIX}/{first:%Y/%m/%d/%H/%M}/"
        stub.add_response("list_object_versions", page, {"Bucket": BUCKET, "Prefix": partition, "MaxKeys": 1000})
        stub.add_response("list_object_versions", page, {"Bucket": BUCKET, "Prefix": partition, "MaxKeys": 1000,
                                                         "KeyMarker": "k", "VersionIdMarker": "v"})
        with pytest.raises(ValueError, match="pagination"):
            search(c)(receipt(), through)
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error("list_object_versions", "AccessDenied", "private", 403)
        with pytest.raises(PermissionError, match="search denied"):
            search(c)(receipt(), through)


def test_object_acknowledged_after_horizon_is_left_for_a_later_poll():
    c, through = client(), START + timedelta(seconds=60)
    late = START + timedelta(seconds=60)  # LastModified + 1 s is after the horizon
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), late, 100)]})
        batch = search(c, through)(receipt(), through)
        stub.assert_no_pending_responses()  # no get_object for the late object
    assert batch.complete and batch.events == ()


def test_registration_and_window_are_fixed():
    c = client()
    for bad in ({"bucket": "Bad_Bucket"}, {"prefix": "/audit"}, {"prefix": "audit/onprem/"}, {"origin": None},
                {"skew": timedelta(minutes=5)}):
        values = {"bucket": BUCKET, "prefix": PREFIX, "region": "us-east-1", "origin": ORIGIN} | bad
        with pytest.raises(ValueError):
            ObjectStoreAuditSearch(c, **values)
    with pytest.raises(ValueError, match="canary window"):
        search(c)(receipt(), START - timedelta(seconds=1))


def test_lapsed_or_missing_retention_is_not_evidence():
    # Review reproduction: after COMPLIANCE retention lapses the mode is still reported and an admin can delete
    now = START + timedelta(minutes=10)
    for retain_until in (now - timedelta(seconds=1), now, None):
        c = client()
        data = export((START + timedelta(seconds=2), login()))
        with Stubber(c) as stub:
            listing(stub, now, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
            fetch(stub, key(MINUTE), data, MODIFIED, retain_until=retain_until)
            with pytest.raises(ValueError, match="retention missing or expired"):
                search(c, now)(receipt(), now)


def test_listing_must_say_it_ended():
    # Review reproduction: no IsTruncated but next markers present used to end the search as complete
    through = START + timedelta(minutes=10)
    first = START - timedelta(minutes=1)
    partition = f"{PREFIX}/{first:%Y/%m/%d/%H/%M}/"
    for page in (
        {"Versions": [], "NextKeyMarker": "k", "NextVersionIdMarker": "v"},
        {"Versions": []},
        {"Versions": [], "IsTruncated": False, "NextKeyMarker": "k", "NextVersionIdMarker": "v"},
        {"Versions": [], "IsTruncated": True},
    ):
        c = client()
        with Stubber(c) as stub:
            stub.add_response("list_object_versions", dict(page, ResponseMetadata={"HTTPStatusCode": 200}),
                              {"Bucket": BUCKET, "Prefix": partition, "MaxKeys": 1000})
            with pytest.raises(ValueError, match="listing end|pagination"):
                search(c)(receipt(), through)


def test_retention_lapsing_while_the_search_reads_is_not_evidence():
    # Review reproduction: retained when the headers arrived, lapsed while the body was read
    through = START + timedelta(minutes=10)
    lapse = through + timedelta(seconds=1)
    now = [lapse - timedelta(seconds=1)]

    class SlowBody(StreamingBody):
        def read(self, *args, **kwargs):
            now[0] = lapse + timedelta(seconds=1)
            return super().read(*args, **kwargs)

    c = client()
    data = export((START + timedelta(seconds=2), login()))
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
        stub.add_response(
            "get_object",
            {"Body": SlowBody(io.BytesIO(data), len(data)), "VersionId": VERSION, "LastModified": MODIFIED,
             "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": lapse, "ContentLength": len(data),
             "ResponseMetadata": {"HTTPStatusCode": 200}},
            {"Bucket": BUCKET, "Key": key(MINUTE), "VersionId": VERSION},
        )
        adapter = ObjectStoreAuditSearch(c, bucket=BUCKET, prefix=PREFIX, region="us-east-1", origin=ORIGIN,
                                         clock=lambda: now[0])
        with pytest.raises(ValueError, match="retention missing or expired"):
            adapter(receipt(), through)


def test_batch_query_time_is_the_time_retention_was_checked():
    through = START + timedelta(minutes=10)
    fixed = through + timedelta(seconds=5)
    c = client()
    data = export((START + timedelta(seconds=2), login()))
    with Stubber(c) as stub:
        listing(stub, through, {MINUTE: [version(key(MINUTE), MODIFIED, len(data))]})
        fetch(stub, key(MINUTE), data, MODIFIED)
        batch = ObjectStoreAuditSearch(c, bucket=BUCKET, prefix=PREFIX, region="us-east-1", origin=ORIGIN,
                                       clock=lambda: fixed)(receipt(), through)
    assert batch.queried_at == fixed
