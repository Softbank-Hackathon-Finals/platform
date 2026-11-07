"""onprem gaps: SeaweedFS (S3-compatible) object probes, raw app-log search, IAM-01.2 mapping."""

import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import botocore.session
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber

from bravo_controls.app_isolation import ClientObjectIsolationReader, S3IsolationReader
from bravo_controls.audit_store import ObjectStoreAuditSearch, ObjectStoreSecretLogSearch, _minutes
from bravo_controls.aws_probes import ObjectTarget, S3AnonymousReadProbe
from bravo_controls.iam_probes import (
    RKE2_RBAC_CHECKS,
    KubeBenchIamProbe,
    rke2_rbac_mapping,
)
from bravo_controls.iam_probes import (
    SPEC as IAM2,
)
from bravo_controls.otlp_audit import AppLogOrigin, AuditOrigin
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec
from bravo_controls.secret_logs import SPEC as LOG_SPEC
from bravo_controls.secret_logs import LogCanaryReceipt, SecretLogProbe
from bravo_controls.tool_reports import ToolRun

ENDPOINT = "https://seaweedfs.storage.svc:8333"
ENV = Environment("onprem-target", "target", "onprem:rke2/fixture", "onprem")
CTX = RunContext("run-1", "demo-chat", ENV)
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
IAM = Spec("IAM-01.1", "IAM-01", "1", "deny", "required")
TARGET = ObjectTarget(
    "demo-chat", ENV, "", "us-east-1", "chat-attach", "fixture/a.png", "etag-1",
    provider="s3-compatible", endpoint_url=ENDPOINT,
)


class S3Error(Exception):
    def __init__(self, code="AccessDenied", status=403):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def s3(*, unsigned=False, endpoint=ENDPOINT):
    config = Config(
        signature_version=UNSIGNED if unsigned else "s3v4",
        connect_timeout=2,
        read_timeout=2,
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    result = Mock()
    result.meta = SimpleNamespace(
        config=config, region_name="us-east-1", endpoint_url=endpoint,
        service_model=SimpleNamespace(service_name="s3"),
    )
    return result


def ok(etag='"etag-1"', version=None):
    body = Mock()
    response = {"ResponseMetadata": {"HTTPStatusCode": 200}, "Body": body, "ETag": etag}
    if version is not None:
        response["VersionId"] = version
    return response


def test_object_target_providers_are_validated():
    with pytest.raises(ValueError):  # SeaweedFS needs a pinned endpoint
        replace(TARGET, endpoint_url=None)
    with pytest.raises(ValueError):  # and has no AWS bucket owner account
        replace(TARGET, account_id="123456789012")
    with pytest.raises(ValueError):
        replace(TARGET, endpoint_url="ftp://seaweedfs:8333")
    with pytest.raises(ValueError):  # aws S3 must not be redirected to another endpoint
        ObjectTarget("a", ENV, "123456789012", "us-east-1", "b-1", "k", "v", endpoint_url=ENDPOINT)
    with pytest.raises(ValueError):  # gcs generation is numeric
        ObjectTarget("a", ENV, "fixture-proj", "us", "b-1", "k", "v", provider="gcs")
    assert TARGET.binding()["endpoint_url"] == ENDPOINT
    legacy = ObjectTarget("a", ENV, "123456789012", "us-east-1", "b-1", "k", "v")
    assert set(legacy.binding()) == {"account_id", "bucket", "key", "version_id"}


def seaweed_probe(app, anonymous):
    return S3AnonymousReadProbe(
        TARGET, app_client=app, anonymous_client=anonymous,
        app_principal_arn="seaweedfs:identity/demo-chat", evidence_sink=[].append,
        clock=lambda: NOW,
    )


def test_seaweedfs_anonymous_get_is_denied_without_bucket_owner_header():
    app, anonymous = s3(), s3(unsigned=True)
    app.get_object.return_value = ok()
    anonymous.get_object.side_effect = S3Error()
    result = seaweed_probe(app, anonymous)(CTX, IAM)
    assert (result.observed, result.positive_control, result.unknown_reason) == ("deny", "pass", None)
    for call in (*app.get_object.call_args_list, *anonymous.get_object.call_args_list):
        assert call.kwargs == {"Bucket": "chat-attach", "Key": "fixture/a.png"}


def test_seaweedfs_versioned_bucket_and_public_object():
    app, anonymous = s3(), s3(unsigned=True)
    target = replace(TARGET, version_id="v-123")
    app.get_object.return_value = ok(version="v-123")
    anonymous.get_object.return_value = ok(version="v-123")
    probe = S3AnonymousReadProbe(
        target, app_client=app, anonymous_client=anonymous,
        app_principal_arn="seaweedfs:identity/demo-chat", evidence_sink=[].append,
        clock=lambda: NOW,
    )
    assert probe(CTX, IAM).observed == "allow"


def test_seaweedfs_client_must_use_the_registered_endpoint():
    with pytest.raises(ValueError):
        seaweed_probe(s3(endpoint="https://elsewhere:8333"), s3(unsigned=True))
    with pytest.raises(ValueError):
        seaweed_probe(s3(), s3())  # anonymous client must be unsigned
    with pytest.raises(ValueError):
        S3AnonymousReadProbe(
            TARGET, app_client=s3(), anonymous_client=s3(unsigned=True),
            app_principal_arn="anonymous", evidence_sink=[].append,
        )


def test_seaweedfs_isolation_leg_and_aws_reader_refuses_it():
    other_target = replace(TARGET, app="other-app", bucket="other-attach")
    owner, other = s3(), s3()
    owner.get_object.return_value = ok()

    def other_get(**params):
        if params["Bucket"] == TARGET.bucket:
            raise S3Error()
        return ok()

    other.get_object.side_effect = other_get
    holder = {}
    reader = ClientObjectIsolationReader(
        TARGET, other_target, owner_client=owner, other_client=other,
        owner_principal="seaweedfs:identity/demo-chat",
        other_principal="seaweedfs:identity/other-app",
        read_registration=lambda _: holder["binding"], clock=lambda: NOW,
    )
    holder["binding"] = reader.binding
    leg = reader(CTX, "nonce")
    assert (leg.positive, leg.outcome) == (True, "deny")
    with pytest.raises(ValueError):
        S3IsolationReader(
            TARGET, other_target, owner_role=None, other_role=None,
            credentials=lambda _: None, read_registration=lambda _: None,
        )


START = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
END, SETTLED = START + timedelta(seconds=30), START + timedelta(seconds=90)
BUCKET, PREFIX = "bravo-audit", "logs/onprem"
APP_ORIGIN = AppLogOrigin(
    "target", "sha256:" + "a" * 64, "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-3333333333ab", "demo-chat", "demo-chat-0", "app",
)
SECRET = "bravo_secret_" + "1" * 64
MARKER = "bravo_log_" + "2" * 32
UUID7 = "01a1a801-c758-7182-83a6-5f55274e5521"
VERSION = "671b122adb390a798abee65cc0967cf6"


def store_client():
    return botocore.session.get_session().create_client(
        "s3", region_name="us-east-1", endpoint_url=ENDPOINT,
        aws_access_key_id="testing", aws_secret_access_key="testing",
        config=Config(
            connect_timeout=2, read_timeout=3,
            retries={"mode": "standard", "total_max_attempts": 1},
            signature_version="s3v4", s3={"addressing_style": "path"},
        ),
    )


def app_export(*lines, origin=APP_ORIGIN):
    attrs = [{"key": k, "value": {"stringValue": v}} for k, v in origin.attributes().items()]
    records = [
        {"timeUnixNano": str(int((START + timedelta(seconds=5)).timestamp()) * 10**9),
         "body": {"stringValue": line}}
        for line in lines
    ]
    return json.dumps(
        {"resourceLogs": [{"resource": {"attributes": attrs},
                           "scopeLogs": [{"scope": {}, "logRecords": records}]}]}
    ).encode()


def log_search(c):
    return ObjectStoreSecretLogSearch(
        c, bucket=BUCKET, prefix=PREFIX, region="us-east-1", origin=APP_ORIGIN,
        clock=lambda: SETTLED + timedelta(seconds=5), endpoint_url=ENDPOINT,
    )


def stub_store(stub, data):
    minute = START
    modified = START + timedelta(seconds=40)
    key = f"{PREFIX}/{minute:%Y/%m/%d/%H/%M}/logs_{UUID7}.json"
    for m in _minutes(START - timedelta(minutes=1), SETTLED + timedelta(minutes=1)):
        rows = [{"Key": key, "VersionId": VERSION, "IsLatest": True, "LastModified": modified,
                 "Size": len(data)}] if m == minute else []
        stub.add_response(
            "list_object_versions",
            {"Versions": rows, "IsTruncated": False, "ResponseMetadata": {"HTTPStatusCode": 200}},
            {"Bucket": BUCKET, "Prefix": f"{PREFIX}/{m:%Y/%m/%d/%H/%M}/", "MaxKeys": 1000},
        )
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data)), "VersionId": VERSION,
         "LastModified": modified, "ObjectLockMode": "COMPLIANCE", "ContentLength": len(data),
         "ObjectLockRetainUntilDate": START + timedelta(days=1),
         "ResponseMetadata": {"HTTPStatusCode": 200}},
        {"Bucket": BUCKET, "Key": key, "VersionId": VERSION},
    )


def run_log_probe(data):
    c = store_client()
    search = log_search(c)
    receipt = LogCanaryReceipt(
        CTX, search.source_binding, search.streams, "sha256:" + "c" * 64, SECRET, MARKER,
        START, END, SETTLED, frozenset({"configured", "exercised", "restored", "logs_drained"}),
    )
    probe = SecretLogProbe(
        receipt_provider=lambda *_: receipt, search=search, source_binding=search.source_binding,
        streams=search.streams, configuration_sha256=receipt.configuration_sha256,
        evidence_sink=[].append, clock=lambda: SETTLED + timedelta(seconds=5),
    )
    with Stubber(c) as stub:
        stub_store(stub, data)
        result = probe(CTX, LOG_SPEC)
        stub.assert_no_pending_responses()
    return result


def test_onprem_raw_app_log_window_from_the_retained_store():
    result = run_log_probe(app_export("started " + MARKER, "GET /api/health 200"))
    assert (result.observed, result.positive_control) == ("absent", "pass")
    assert run_log_probe(app_export(MARKER, "db password=" + SECRET)).observed == "present"
    result = run_log_probe(app_export("no marker here"))
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"


def test_app_log_export_from_another_container_is_rejected():
    other = replace(APP_ORIGIN, container="sidecar")
    with pytest.raises(ValueError, match="registered app"):
        run_log_probe(app_export(MARKER, origin=other))


def test_log_and_audit_searches_take_only_their_own_origin():
    audit_origin = AuditOrigin(
        "target", "sha256:" + "c" * 64, "22222222-2222-4222-8222-222222222222",
        "11111111-1111-4111-8111-111111111111", "target-dev", "target-db-1",
    )
    with pytest.raises(ValueError):
        ObjectStoreSecretLogSearch(store_client(), bucket=BUCKET, prefix=PREFIX,
                                   region="us-east-1", origin=audit_origin)
    with pytest.raises(ValueError):
        ObjectStoreAuditSearch(store_client(), bucket=BUCKET, prefix=PREFIX,
                               region="us-east-1", origin=APP_ORIGIN)
    with pytest.raises(ValueError):  # pinned endpoint must match the client
        ObjectStoreAuditSearch(store_client(), bucket=BUCKET, prefix=PREFIX, region="us-east-1",
                               origin=audit_origin, endpoint_url="https://elsewhere:8333")


FIXTURE = Path(__file__).parent / "fixtures" / "kube-bench" / "rke2-cis-1.7.json"


def kube_probe(runs, targets=("node-a",)):
    mapping = rke2_rbac_mapping("rke2:fixture/policies", targets)
    evidence = []
    probe = KubeBenchIamProbe(
        collect_runs=lambda _: runs(mapping), mapping=mapping,
        evidence_sink=evidence.append, clock=lambda: NOW,
    )
    return probe, mapping, evidence


def test_onprem_iam_mapping_on_captured_rke2_output_is_unknown_never_pass():
    def runs(mapping):
        return (ToolRun("kube-bench", "0.10.2", mapping.scope, "node-a", FIXTURE.read_bytes(), 0, True),)

    probe, mapping, evidence = kube_probe(runs)
    assert mapping.required == tuple((check, "node-a") for check in RKE2_RBAC_CHECKS)
    result = probe(CTX, IAM2)
    assert (result.observed, result.unknown_reason) == (None, "UNEXPECTED_RESPONSE")
    assert result.tool == "kube-bench" and json.loads(evidence[0])["summary"]
    probe, _, _ = kube_probe(lambda _: ())
    assert probe(CTX, IAM2).unknown_reason == "NOT_RUN"
