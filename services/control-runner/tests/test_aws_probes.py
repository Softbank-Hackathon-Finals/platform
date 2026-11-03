import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

from bravo_controls.aws_probes import (
    TOOL,
    TOOL_VERSION,
    ObjectTarget,
    S3AnonymousReadProbe,
    SecretAccessProbe,
    SecretTarget,
)
from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.runner import TestSpec as ControlTestSpec

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
APP_ROLE = "arn:aws:iam::123456789012:role/fixture-app"
OTHER_ROLE = "arn:aws:iam::123456789012:role/fixture-other"
ENV = Environment("fixture-source", "source", "registered/source")
CONTEXT = RunContext("fixture-run-1", "demo-chat", ENV)
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
VERSION = "a" * 32
SECRET = SecretTarget(
    "demo-chat",
    ENV,
    "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:fixture-db-abcdef",
    VERSION,
)
OBJECT = ObjectTarget(
    "demo-chat", ENV, ACCOUNT, REGION, "fixture-bucket", "fixture/object", VERSION
)
CANARY = "DO-NOT-PERSIST-SECRET-CANARY"


class AwsError(Exception):
    def __init__(self, code="AccessDenied", status=403):
        self.response = {
            "Error": {"Code": code, "Message": CANARY},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": CANARY},
        }

    def __str__(self):
        raise AssertionError("exception text must not be read")


def client(service, *, unsigned=False):
    config = Config(
        signature_version=UNSIGNED if unsigned else "v4",
        connect_timeout=2,
        read_timeout=2,
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    result = Mock()
    result.meta = SimpleNamespace(
        config=config, region_name=REGION, service_model=SimpleNamespace(service_name=service)
    )
    return result


def secret_response(**changes):
    return {
        "ARN": SECRET.secret_arn,
        "VersionId": VERSION,
        "VersionStages": ["AWSCURRENT"],
        "SecretString": CANARY,
        "SecretBinary": CANARY.encode(),
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": CANARY},
        **changes,
    }


def object_response(**changes):
    body = Mock()
    body.read.side_effect = AssertionError("object contents must not be read")
    return {
        "VersionId": VERSION,
        "Body": body,
        "ContentLength": len(CANARY),
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": CANARY},
        **changes,
    }


def setup_probe(kind, *, sink=None):
    retained = []
    app = client("secretsmanager" if kind == "secret" else "s3")
    other = client("secretsmanager" if kind == "secret" else "s3", unsigned=kind == "object")
    if kind == "secret":
        app.get_secret_value.return_value = secret_response()
        other.get_secret_value.side_effect = AwsError("AccessDeniedException", 400)
        probe = SecretAccessProbe(
            SECRET,
            app_client=app,
            other_client=other,
            app_principal_arn=APP_ROLE,
            other_principal_arn=OTHER_ROLE,
            evidence_sink=sink or retained.append,
            clock=lambda: NOW,
        )
    else:
        app.get_object.return_value = object_response()
        other.get_object.side_effect = AwsError()
        probe = S3AnonymousReadProbe(
            OBJECT,
            app_client=app,
            anonymous_client=other,
            app_principal_arn=APP_ROLE,
            evidence_sink=sink or retained.append,
            clock=lambda: NOW,
        )
    return probe, app, other, retained


def spec(kind):
    control = "SEC-01" if kind == "secret" else "IAM-01"
    return ControlTestSpec(control + ".1", control, "1", "deny", "required")


def test_registered_aws_collectors_produce_complete_t5_run_with_unrun_audit():
    root = Path(__file__).resolve().parents[3] / "docs/controls"
    catalog = Catalog(
        json.loads((root / "profiles.json").read_text()),
        json.loads((root / "control-result.schema.json").read_text()),
    )
    secret, _, _, secret_evidence = setup_probe("secret")
    bucket, _, _, object_evidence = setup_probe("object")
    run = Runner(catalog, {"SEC-01.1": secret, "IAM-01.1": bucket}, clock=lambda: NOW).run(
        profile_id="demo-gate",
        profile_version="1",
        app="demo-chat",
        environment=ENV,
    )
    assert [r["verdict"] for r in run["results"]] == ["PASS", "PASS", "UNKNOWN"]
    assert run["results"][2]["unknown_reason"] == "NOT_RUN"
    for data in (secret_evidence[0], object_evidence[0]):
        assert json.loads(data)["run_id"] == run["run_id"]
    assert CANARY not in json.dumps(run)


@pytest.mark.parametrize("kind", ["secret", "object"])
def test_positive_then_negative_use_same_registered_resource_and_version(kind, capsys, caplog):
    probe, app, other, retained = setup_probe(kind)
    calls = Mock()
    method = "get_secret_value" if kind == "secret" else "get_object"
    calls.attach_mock(getattr(app, method), "positive")
    calls.attach_mock(getattr(other, method), "negative")
    result = probe(CONTEXT, spec(kind))
    args = (
        {"SecretId": SECRET.secret_arn, "VersionId": VERSION, "VersionStage": "AWSCURRENT"}
        if kind == "secret"
        else {"Bucket": OBJECT.bucket, "Key": OBJECT.key, "ExpectedBucketOwner": ACCOUNT}
    )
    assert calls.mock_calls[:2] == [call.positive(**args), call.negative(**args)]
    assert result.observed == "deny" and result.positive_control == "pass"
    assert result.unknown_reason is None
    assert (result.tool, result.tool_version) == (TOOL, TOOL_VERSION)
    assert result.output_sha256 == "sha256:" + hashlib.sha256(retained[0]).hexdigest()
    evidence = json.loads(retained[0])
    assert evidence["run_id"] == CONTEXT.run_id
    assert evidence["environment"] == {"id": ENV.id, "role": ENV.role, "ref": ENV.ref}
    assert CANARY not in repr(result) + retained[0].decode() + caplog.text + str(
        capsys.readouterr()
    )
    if kind == "object":
        assert app.get_object.call_count == 2
        assert calls.mock_calls == [
            call.positive(**args),
            call.negative(**args),
            call.positive(**args),
        ]
        assert app.get_object.return_value["Body"].close.call_count == 2
        app.get_object.return_value["Body"].read.assert_not_called()


@pytest.mark.parametrize("kind", ["secret", "object"])
def test_unexpected_access_is_allow_not_denial_and_secret_contents_never_escape(kind):
    probe, _, other, retained = setup_probe(kind)
    method = other.get_secret_value if kind == "secret" else other.get_object
    method.side_effect = None
    method.return_value = secret_response() if kind == "secret" else object_response()
    result = probe(CONTEXT, spec(kind))
    assert result.observed == "allow" and result.unknown_reason is None
    assert CANARY not in repr(result) + retained[0].decode()
    if kind == "object":
        method.return_value["Body"].close.assert_called_once_with()
        method.return_value["Body"].read.assert_not_called()


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize(
    "code,status",
    [
        ("NoSuchKey", 404),
        ("AccessDenied", 500),
        ("ExpiredToken", 403),
        ("InvalidClientTokenId", 403),
        ("AccessDeniedExtra", 403),
        ("", 403),
        ([], 403),
        ("AccessDenied", None),
        ("AccessDenied", "403"),
    ],
)
def test_non_authorization_errors_never_count_as_denial(kind, code, status):
    probe, _, other, retained = setup_probe(kind)
    method = other.get_secret_value if kind == "secret" else other.get_object
    method.side_effect = AwsError(code, status)
    result = probe(CONTEXT, spec(kind))
    assert result.observed is None and result.unknown_reason == "TOOL_ERROR"
    assert result.positive_control == "pass"
    assert CANARY not in retained[0].decode()


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize("error", [TimeoutError(CANARY), ReadTimeoutError(endpoint_url=CANARY)])
def test_timeouts_are_unknown_without_exception_text(kind, error):
    probe, _, other, retained = setup_probe(kind)
    method = other.get_secret_value if kind == "secret" else other.get_object
    method.side_effect = error
    result = probe(CONTEXT, spec(kind))
    assert result.observed is None and result.unknown_reason == "TIMEOUT"
    assert CANARY not in repr(result) + retained[0].decode()


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize("error", [AwsError(), TimeoutError(CANARY), AwsError("NoSuchKey", 404)])
def test_failed_positive_control_stops_negative_test(kind, error):
    probe, app, other, retained = setup_probe(kind)
    method = app.get_secret_value if kind == "secret" else app.get_object
    method.side_effect = error
    result = probe(CONTEXT, spec(kind))
    assert result.observed is None and result.positive_control == "fail"
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert json.loads(retained[0])["negative"]["reason"] == "NOT_RUN"
    other.get_secret_value.assert_not_called()
    other.get_object.assert_not_called()


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize(
    "context",
    [
        replace(CONTEXT, app="other-app"),
        replace(CONTEXT, environment=Environment("another", "source", ENV.ref)),
        replace(CONTEXT, environment=Environment(ENV.id, "target", ENV.ref)),
        replace(CONTEXT, environment=Environment(ENV.id, "source", "new/ref")),
        replace(CONTEXT, run_id="bad\nrun"),
    ],
)
def test_scope_mismatch_fails_before_sdk_or_evidence(kind, context):
    probe, app, other, retained = setup_probe(kind)
    with pytest.raises(ValueError, match="context or method mismatch"):
        probe(context, spec(kind))
    assert not app.mock_calls and not other.mock_calls and not retained


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize(
    "changes",
    [
        {"method_version": "2"},
        {"expected": "allow"},
        {"test_id": "AUD-01.1"},
        {"positive_control": "not_applicable"},
    ],
)
def test_method_is_pinned_on_server(kind, changes):
    probe, app, other, retained = setup_probe(kind)
    with pytest.raises(ValueError):
        probe(CONTEXT, replace(spec(kind), **changes))
    assert not app.mock_calls and not other.mock_calls and not retained


@pytest.mark.parametrize(
    "config",
    [
        Config(),
        Config(connect_timeout=60, read_timeout=2),
        Config(connect_timeout=2, read_timeout=float("inf")),
        Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 10}),
        Config(
            connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1, "mode": "adaptive"}
        ),
    ],
)
def test_unbounded_client_is_rejected_and_mutation_is_rechecked(config):
    probe, app, other, retained = setup_probe("secret")
    app.meta.config = config
    with pytest.raises(ValueError, match="bounded server-registered"):
        probe(CONTEXT, spec("secret"))
    assert not app.mock_calls and not other.mock_calls and not retained


def test_unsigned_identity_is_required_not_a_string_or_authenticated_client():
    probe, app, anonymous, retained = setup_probe("object")
    for signature in ("unsigned", "v4", None):
        anonymous.meta.config.signature_version = signature
        with pytest.raises(ValueError, match="bounded server-registered"):
            probe(CONTEXT, spec("object"))
    assert not app.mock_calls and not anonymous.mock_calls and not retained


@pytest.mark.parametrize(
    "other_role",
    [
        APP_ROLE,
        "not-a-role",
        APP_ROLE.replace(ACCOUNT, "111122223333"),
        "arn:aws:sts::123456789012:assumed-role/fixture-app/another-session",
    ],
)
def test_distinct_registered_role_ids_required(other_role):
    with pytest.raises(ValueError):
        SecretAccessProbe(
            SECRET,
            app_client=client("secretsmanager"),
            other_client=client("secretsmanager"),
            app_principal_arn=APP_ROLE,
            other_principal_arn=other_role,
            evidence_sink=lambda _: None,
        )


def test_same_client_object_cannot_be_positive_and_negative():
    shared = client("secretsmanager")
    with pytest.raises(ValueError, match="independent clients"):
        SecretAccessProbe(
            SECRET,
            app_client=shared,
            other_client=shared,
            app_principal_arn=APP_ROLE,
            other_principal_arn=OTHER_ROLE,
            evidence_sink=lambda _: None,
        )


@pytest.mark.parametrize("kind", ["secret", "object"])
def test_retention_failure_never_returns_a_digest_without_evidence(kind):
    sink = Mock(side_effect=RuntimeError(CANARY))
    probe, _, _, _ = setup_probe(kind, sink=sink)
    with pytest.raises(ValueError, match="^collector evidence retention failed$") as caught:
        probe(CONTEXT, spec(kind))
    assert CANARY not in str(caught.value)
    sink.assert_called_once()


@pytest.mark.parametrize("kind", ["secret", "object"])
def test_evidence_hash_is_bound_to_run_and_retained_bytes(kind):
    probe, _, _, retained = setup_probe(kind)
    first = probe(CONTEXT, spec(kind))
    second = probe(replace(CONTEXT, run_id="fixture-run-2"), spec(kind))
    assert first.output_sha256 != second.output_sha256
    assert len(retained) == 2
    for result, encoded in zip((first, second), retained, strict=True):
        assert result.output_sha256 == "sha256:" + hashlib.sha256(encoded).hexdigest()
        assert json.loads(encoded)["run_id"] == result.context.run_id


@pytest.mark.parametrize(
    "changes",
    [
        {"ARN": SECRET.secret_arn + "other"},
        {"VersionId": "b" * 32},
        {"VersionStages": ["AWSPREVIOUS"]},
        {"ResponseMetadata": {"HTTPStatusCode": 202}},
    ],
)
def test_secret_positive_resource_and_version_must_match(changes):
    probe, app, other, _ = setup_probe("secret")
    app.get_secret_value.return_value = secret_response(**changes)
    result = probe(CONTEXT, spec("secret"))
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    other.get_secret_value.assert_not_called()


def test_s3_body_is_closed_even_when_response_version_is_wrong():
    probe, app, other, _ = setup_probe("object")
    app.get_object.return_value = object_response(VersionId="wrong")
    result = probe(CONTEXT, spec("object"))
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    app.get_object.return_value["Body"].close.assert_called_once_with()
    app.get_object.return_value["Body"].read.assert_not_called()
    other.get_object.assert_not_called()


def test_s3_body_close_failure_is_unknown_without_leaking_exception():
    probe, app, other, retained = setup_probe("object")
    app.get_object.return_value["Body"].close.side_effect = RuntimeError(CANARY)
    result = probe(CONTEXT, spec("object"))
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert CANARY not in retained[0].decode()
    other.get_object.assert_not_called()


@pytest.mark.parametrize("kind", ["secret", "object"])
@pytest.mark.parametrize("response", [None, {}, "sensitive-response"])
def test_malformed_response_is_unknown(kind, response):
    probe, app, _, _ = setup_probe(kind)
    method = app.get_secret_value if kind == "secret" else app.get_object
    method.return_value = response
    result = probe(CONTEXT, spec(kind))
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"


def test_s3_requires_immutable_object_version():
    for version in ("", "null", "version\n"):
        with pytest.raises(ValueError):
            replace(OBJECT, version_id=version)


def test_public_current_object_is_not_misreported_by_denied_get_object_version():
    probe, _, anonymous, _ = setup_probe("object")

    def bucket_policy(**kwargs):
        if "VersionId" in kwargs:
            raise AwsError("AccessDenied", 403)
        return object_response()

    anonymous.get_object.side_effect = bucket_policy
    result = probe(CONTEXT, spec("object"))
    assert result.observed == "allow" and result.unknown_reason is None
    assert "VersionId" not in anonymous.get_object.call_args.kwargs


@pytest.mark.parametrize(
    "second", [AwsError("NoSuchKey", 404), object_response(VersionId="new-version")]
)
def test_s3_denial_is_unknown_if_object_disappears_or_changes_after_positive(second):
    probe, app, anonymous, retained = setup_probe("object")
    first = object_response()
    app.get_object.side_effect = [first, second]
    anonymous.get_object.side_effect = AwsError("AccessDenied", 403)
    result = probe(CONTEXT, spec("object"))
    assert result.observed is None and result.positive_control == "fail"
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert app.get_object.call_count == 2 and anonymous.get_object.call_count == 1
    evidence = json.loads(retained[0])
    assert evidence["positive"]["outcome"] == "allow"
    assert evidence["negative"]["outcome"] == "deny"
    assert evidence["postpositive"]["outcome"] is None
    assert evidence["postpositive"]["reason"] in {"TOOL_ERROR", "UNEXPECTED_RESPONSE"}
    first["Body"].close.assert_called_once_with()
    if isinstance(second, dict):
        second["Body"].close.assert_called_once_with()


def test_s3_success_requires_second_positive_and_retains_its_result():
    probe, app, _, retained = setup_probe("object")
    first, second = object_response(), object_response()
    app.get_object.side_effect = [first, second]
    result = probe(CONTEXT, spec("object"))
    assert result.observed == "deny" and result.positive_control == "pass"
    assert app.get_object.call_count == 2
    assert json.loads(retained[0])["postpositive"] == {
        "outcome": "allow",
        "http_status": 200,
        "reason": None,
    }
    first["Body"].close.assert_called_once_with()
    second["Body"].close.assert_called_once_with()
