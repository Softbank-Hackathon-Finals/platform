import io
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.session import Session
from botocore.stub import Stubber

from bravo_controls.app_isolation import SPEC, AccessLeg, AppIsolationProbe, S3IsolationReader
from bravo_controls.aws_identity import RegisteredRole
from bravo_controls.aws_probes import ObjectTarget
from bravo_controls.runner import Catalog, Environment, RunContext, Runner

ENV = Environment("source", "source", "fixture/source")
CTX = RunContext("isolation-run", "chat", ENV)
NOW = datetime.now(UTC)
OWNER = ObjectTarget("chat", ENV, "123456789012", "ap-northeast-2", "fixture-chat", "canary", "v1")
OTHER = replace(OWNER, app="other", bucket="fixture-other")
ROLE = RegisteredRole("arn:aws:iam::123456789012:role/fixture-chat", "AROA" + "A" * 17)
OTHER_ROLE = RegisteredRole("arn:aws:iam::123456789012:role/fixture-other", "AROA" + "B" * 17)


def leg_reader(outcome="deny", reason=None, positive=True, binding="sha256:" + "a" * 64):
    reader = Mock(app="chat", other_app="other", environment=ENV, binding=binding)
    reader.side_effect = lambda ctx, nonce: AccessLeg(
        ctx, nonce, reader.binding, positive, outcome, reason, datetime.now(UTC), {"cross": outcome}
    )
    return reader


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("deny", "deny", "deny"),
        ("allow", "deny", "allow"),
        ("deny", "allow", "allow"),
        ("allow", "allow", "allow"),
        (None, "deny", None),
        ("deny", None, None),
        ("allow", None, "allow"),
    ],
)
def test_both_required_and_real_allow_is_fail(a, b, expected):
    saved = []
    probe = AppIsolationProbe(
        object_reader=leg_reader(a, None if a else "TOOL_ERROR"),
        database_reader=leg_reader(b, None if b else "TIMEOUT"),
        evidence_sink=saved.append,
    )
    result = probe(CTX, SPEC)
    assert result.observed == expected
    assert len(json.loads(saved[0])["legs"]) == 2


def test_failed_positive_overrides_apparent_denial():
    result = AppIsolationProbe(
        object_reader=leg_reader(),
        database_reader=leg_reader(None, "POSITIVE_CONTROL_FAILED", False),
        evidence_sink=lambda _: None,
    )(CTX, SPEC)
    assert result.observed is None and result.positive_control == "fail"


@pytest.mark.parametrize(
    "field,value",
    [("app", "another"), ("other_app", "third"), ("environment", replace(ENV, id="different"))],
)
def test_mixed_pairs_rejected(field, value):
    reader = leg_reader()
    setattr(reader, field, value)
    with pytest.raises(ValueError):
        AppIsolationProbe(
            object_reader=leg_reader(), database_reader=reader, evidence_sink=lambda _: None
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "replay"},
        {"binding": "wrong"},
        {"context": replace(CTX, run_id="other")},
        {"positive": 1},
        {"outcome": "success"},
        {"reason": "private error"},
        {"collected_at": NOW - timedelta(hours=1)},
        {"collected_at": NOW + timedelta(hours=1)},
        {"outcome": None, "reason": None},
    ],
)
def test_bad_leg_cannot_pass(changes):
    reader = leg_reader()
    original = reader.side_effect
    reader.side_effect = lambda *a: replace(original(*a), **changes)
    with pytest.raises(ValueError):
        AppIsolationProbe(
            object_reader=reader, database_reader=leg_reader(), evidence_sink=lambda _: None
        )(CTX, SPEC)


def test_sink_failure_no_result():
    def fail(_):
        raise PermissionError("fixture")

    with pytest.raises(PermissionError):
        AppIsolationProbe(
            object_reader=leg_reader(), database_reader=leg_reader(), evidence_sink=fail
        )(CTX, SPEC)


def test_full_t5_runner_keeps_other_tests_unknown():
    root = Path(__file__).resolve().parents[3] / "docs/controls"
    catalog = Catalog(
        json.loads((root / "profiles.json").read_text()),
        json.loads((root / "control-result.schema.json").read_text()),
    )
    probe = AppIsolationProbe(
        object_reader=leg_reader(), database_reader=leg_reader(), evidence_sink=lambda _: None
    )
    result = Runner(catalog, {"ISO-01.1": probe}).run(
        profile_id="full", profile_version="1", app="chat", environment=ENV
    )
    assert next(r for r in result["results"] if r["test_id"] == "ISO-01.1")["verdict"] == "PASS"
    assert sum(r["verdict"] == "UNKNOWN" for r in result["results"]) == 19


def sdk_client():
    return Session().create_client(
        "s3",
        region_name=OWNER.region,
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
        endpoint_url="https://s3.invalid",
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )


@pytest.fixture
def s3(monkeypatch):
    owner, other = sdk_client(), sdk_client()
    a, b = Stubber(owner), Stubber(other)
    identities = []

    @contextmanager
    def verified(role, credentials, service, region, **kwargs):
        identities.append(role)
        assert service == "s3" and region == OWNER.region
        yield (owner if role == ROLE else other), {"verified_fixture": role.role_id}

    monkeypatch.setattr("bravo_controls.app_isolation.verified_client", verified)
    reader = S3IsolationReader(
        OWNER,
        OTHER,
        owner_role=ROLE,
        other_role=OTHER_ROLE,
        credentials=lambda role: role,
        read_registration=lambda _: reader.binding,
    )
    with a, b:
        yield reader, a, b, identities
    owner.close()
    other.close()


def expected(target):
    return {"Bucket": target.bucket, "Key": target.key, "ExpectedBucketOwner": target.account_id}


def allow(stub, target, version="v1"):
    stream = io.BytesIO(b"SECRET-CANARY-NOT-TO-READ")
    body = StreamingBody(stream, len(stream.getvalue()))
    body.read = Mock(side_effect=AssertionError("object payload must not be read"))
    stub.add_response(
        "get_object",
        {"VersionId": version, "Body": body, "ResponseMetadata": {"HTTPStatusCode": 200}},
        expected(target),
    )
    return stream


def deny(stub, target, code="AccessDenied", status=403):
    stub.add_client_error(
        "get_object",
        code,
        "SECRET-ERROR-MUST-NOT-PERSIST",
        status,
        expected_params=expected(target),
    )


@pytest.mark.parametrize(
    "negative,expected_outcome",
    [("deny", "deny"), ("allow", "allow"), ("ExpiredToken", None), ("NoSuchKey", None)],
)
def test_s3_real_sdk_requests_and_both_positive_controls(s3, negative, expected_outcome):
    reader, a, b, identities = s3
    streams = [allow(a, OWNER), allow(b, OTHER)]
    if negative == "allow":
        streams.append(allow(b, OWNER))
    else:
        deny(
            b,
            OWNER,
            "AccessDenied" if negative == "deny" else negative,
            403 if negative != "NoSuchKey" else 404,
        )
    streams.extend([allow(a, OWNER), allow(b, OTHER)])
    result = reader(CTX, "nonce")
    assert result.outcome == expected_outcome and result.positive is True
    assert identities == [ROLE, OTHER_ROLE]
    assert all(stream.closed for stream in streams)
    assert "SECRET" not in repr(result)
    a.assert_no_pending_responses()
    b.assert_no_pending_responses()


def test_s3_other_credentials_invalid_never_denial(s3):
    reader, a, b, _ = s3
    allow(a, OWNER)
    deny(b, OTHER, "ExpiredToken")
    allow(a, OWNER)
    deny(b, OTHER, "ExpiredToken")
    result = reader(CTX, "nonce")
    assert result.outcome is None and not result.positive
    b.assert_no_pending_responses()


def test_s3_deleted_or_changed_version_after_rejection_unknown(s3):
    reader, a, b, _ = s3
    allow(a, OWNER)
    allow(b, OTHER)
    deny(b, OWNER)
    allow(a, OWNER, "v2")
    allow(b, OTHER)
    result = reader(CTX, "nonce")
    assert result.outcome is None and result.reason == "POSITIVE_CONTROL_FAILED"


def test_s3_registration_changed_does_not_pass(s3):
    reader, *_ = s3
    reader.registration = lambda _: "other"
    with pytest.raises(ValueError, match="registration"):
        reader(CTX, "nonce")


@pytest.mark.parametrize(
    "changes",
    [{"other": OWNER}, {"other": replace(OTHER, bucket=OWNER.bucket)}, {"other_role": ROLE}],
)
def test_s3_distinct_identity_and_resources_required(changes):
    args = dict(
        owner=OWNER,
        other=OTHER,
        owner_role=ROLE,
        other_role=OTHER_ROLE,
        credentials=lambda _: None,
        read_registration=lambda _: None,
    )
    args.update(changes)
    with pytest.raises(ValueError):
        S3IsolationReader(**args)
