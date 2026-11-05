import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from botocore.config import Config
from botocore.session import Session
from botocore.stub import Stubber

from bravo_controls import secret_encryption as encryption
from bravo_controls.aws_identity import RegisteredRole, TemporaryCredentials
from bravo_controls.aws_probes import SecretTarget
from bravo_controls.runner import Environment, RunContext

NOW = datetime(2026, 11, 5, tzinfo=UTC)
ENV = Environment("fixture-source", "source", "aws:fixture")
CONTEXT = RunContext("encryption-fixture", "demo-chat", ENV)
SECRET = SecretTarget("demo-chat", ENV, "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:app/fixture-abcdef", "2c664a7083044475af7165777ccead9e")
KEY = "arn:aws:kms:ap-northeast-2:123456789012:key/11111111-1111-1111-1111-111111111111"
ROLE = RegisteredRole("arn:aws:iam::123456789012:role/metadata-reader", "AROA" + "A" * 17)
CANARY = "private-credential-must-never-appear"
CREDS = TemporaryCredentials("ASIA" + "A" * 16, CANARY, CANARY, NOW + timedelta(hours=1))
TARGET = encryption.SecretEncryptionTarget(SECRET, KEY)


@pytest.fixture
def fixture(monkeypatch):
    session = Session()
    clients = {name: session.create_client(name, region_name=TARGET.region,
        aws_access_key_id=CREDS.access_key_id, aws_secret_access_key=CREDS.secret_access_key,
        aws_session_token=CREDS.session_token,
        config=Config(retries={"mode": "standard", "total_max_attempts": 1}))
        for name in ("secretsmanager", "kms")}
    stubs = {name: Stubber(client) for name, client in clients.items()}
    calls, output = [], []

    @contextmanager
    def verified(role, credentials, service, region, **kwargs):
        assert role == ROLE and credentials is CREDS and region == TARGET.region
        calls.append(service)
        yield clients[service], {"role_id_sha256": "a" * 64}

    monkeypatch.setattr(encryption, "verified_client", verified)
    for stub in stubs.values():
        stub.activate()
    provider = Mock(return_value=CREDS)
    probe = encryption.SecretEncryptionProbe(TARGET, reader_role=ROLE, credential_provider=provider,
        evidence_sink=output.append, clock=lambda: NOW)
    yield probe, stubs, output, calls, provider
    for name, stub in stubs.items():
        stub.deactivate()
        clients[name].close()


def add_read(stubs, *, default=False, secret_change=None, key_change=None, aliases=None):
    secret = dict(ARN=SECRET.secret_arn, VersionIdsToStages={SECRET.version_id: ["AWSCURRENT"]},
                  ResponseMetadata={"HTTPStatusCode": 200})
    if not default:
        secret["KmsKeyId"] = KEY
    secret.update(secret_change or {})
    stubs["secretsmanager"].add_response("describe_secret", secret, {"SecretId": SECRET.secret_arn})
    key = dict(Arn=KEY, KeyId=TARGET.key_id, AWSAccountId="123456789012", Enabled=True,
               KeyState="Enabled", KeySpec="SYMMETRIC_DEFAULT", KeyUsage="ENCRYPT_DECRYPT",
               KeyManager="AWS" if default else "CUSTOMER")
    key.update(key_change or {})
    stubs["kms"].add_response("describe_key", dict(KeyMetadata=key, ResponseMetadata={"HTTPStatusCode": 200}), {"KeyId": KEY})
    if default:
        response = dict(Aliases=[dict(AliasName="alias/aws/secretsmanager", TargetKeyId=TARGET.key_id)],
                        Truncated=False, ResponseMetadata={"HTTPStatusCode": 200})
        response.update(aliases or {})
        stubs["kms"].add_response("list_aliases", response, {"KeyId": KEY, "Limit": 50})


@pytest.mark.parametrize("default", [False, True])
def test_metadata_only_binds_current_version_existing_key_and_rechecks(fixture, default):
    probe, stubs, output, calls, provider = fixture
    add_read(stubs, default=default)
    add_read(stubs, default=default)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed == "enabled" and result.unknown_reason is None
    provider.assert_called_once_with(CONTEXT, "encryption-reader", ROLE)
    assert calls == ["secretsmanager", "kms"]
    for stub in stubs.values():
        stub.assert_no_pending_responses()
    evidence = json.loads(output[0])
    assert evidence["facts"]["default_aws_key"] is default
    assert evidence["run_id"] == CONTEXT.run_id
    assert result.output_sha256 == encryption.digest(output[0])
    for value in (CANARY, CREDS.access_key_id, SECRET.secret_arn, SECRET.version_id, KEY):
        assert value not in output[0].decode()


@pytest.mark.parametrize("change", [
    {"secret_change": {"ARN": SECRET.secret_arn + "-other"}},
    {"secret_change": {"VersionIdsToStages": {"b" * 32: ["AWSCURRENT"]}}},
    {"secret_change": {"DeletedDate": NOW}},
    {"secret_change": {"KmsKeyId": "alias/customer"}},
    {"key_change": {"Arn": KEY.replace("11111111", "22222222", 1)}},
    {"key_change": {"AWSAccountId": "111122223333"}},
    {"key_change": {"KeyState": "Disabled", "Enabled": False}},
    {"key_change": {"KeyUsage": "SIGN_VERIFY"}},
    {"default": True, "aliases": {"Aliases": []}},
    {"default": True, "aliases": {"Aliases": [{"AliasName": "alias/aws/secretsmanager", "TargetKeyId": "different-key"}] }},
])
def test_unknown_never_claims_encryption_when_binding_or_key_unconfirmed(fixture, change):
    probe, stubs, output, _, _ = fixture
    add_read(stubs, **change)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"
    assert json.loads(output[0])["facts"] == {}


def test_change_between_readbacks_blocks(fixture):
    probe, stubs, output, _, _ = fixture
    add_read(stubs)
    add_read(stubs, secret_change={"VersionIdsToStages": {"b" * 32: ["AWSCURRENT"]}})
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None
    assert json.loads(output[0])["facts"] == {}


def test_access_denied_is_not_plaintext_or_a_pass(fixture):
    probe, stubs, output, _, _ = fixture
    stubs["secretsmanager"].add_client_error("describe_secret", "AccessDeniedException", CANARY, http_status_code=400)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "OBSERVATION_DENIED"
    assert CANARY not in output[0].decode()


def test_scope_mismatch_has_no_credential_or_sdk_call(fixture):
    probe, _, output, calls, provider = fixture
    with pytest.raises(ValueError, match="scope_or_method"):
        probe(replace(CONTEXT, app="other-app"), encryption.SPEC)
    assert not output and not calls
    provider.assert_not_called()


def test_store_failure_cannot_return_positive_observation(fixture):
    probe, stubs, _, _, _ = fixture
    add_read(stubs)
    add_read(stubs)
    probe.sink = Mock(side_effect=OSError(CANARY))
    with pytest.raises(OSError):
        probe(CONTEXT, encryption.SPEC)


def test_clock_reversal_is_unknown(fixture):
    probe, stubs, output, _, _ = fixture
    add_read(stubs)
    add_read(stubs)
    readings = iter([NOW, NOW - timedelta(seconds=1), NOW])
    probe.clock = lambda: next(readings)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"
    assert len(output) == 1


def test_expired_during_collection_is_unknown(fixture):
    probe, stubs, _, _, _ = fixture
    add_read(stubs)
    add_read(stubs)
    expired = CREDS.expires_at
    readings = iter([NOW, expired, expired])
    probe.clock = lambda: next(readings)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"


def test_time_budget_stops_before_metadata_request(fixture):
    probe, _, output, _, _ = fixture
    readings = iter([0, 61])
    probe.monotonic = lambda: next(readings)
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "TIMEOUT"
    assert len(output) == 1


def test_incomplete_alias_listing_cannot_confirm_default_key(fixture):
    probe, stubs, _, _, _ = fixture
    add_read(stubs, default=True, aliases={"Truncated": True})
    result = probe(CONTEXT, encryption.SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"


def test_cross_account_key_registration_rejected():
    with pytest.raises(ValueError, match="same_region_account"):
        encryption.SecretEncryptionTarget(SECRET, KEY.replace("123456789012", "111122223333"))
