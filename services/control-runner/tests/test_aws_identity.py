import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from botocore import UNSIGNED
from botocore.exceptions import ClientError
from botocore.session import Session
from botocore.stub import Stubber

from bravo_controls import aws_identity as identity
from bravo_controls.aws_identity import (
    IdentityUnavailable,
    RegisteredRole,
    TemporaryCredentials,
    anonymous_s3_client,
    validate_identity,
    verified_client,
)

NOW = datetime(2026, 11, 4, tzinfo=UTC)
ROLE = RegisteredRole('arn:aws:iam::123456789012:role/apps/fixture-app', 'AROA' + 'A' * 17)
CANARY = 'never-log-this-credential-canary'
CREDS = TemporaryCredentials('ASIA' + 'A' * 16, CANARY, CANARY, NOW + timedelta(minutes=15))
RESPONSE = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/fixture-app/fixture-session',
            'UserId': ROLE.role_id + ':fixture-session',
            'ResponseMetadata': {'HTTPStatusCode': 200}}


@pytest.fixture
def sdk(monkeypatch):
    session = Session()
    original = session.create_client
    created = []
    stubs = []

    def create(service, **kwargs):
        client = original(service, **kwargs)
        created.append((service, kwargs, client))
        if service == 'sts':
            stub = Stubber(client)
            stub.add_response('get_caller_identity', RESPONSE, {})
            stub.activate()
            stubs.append(stub)
        return client

    monkeypatch.setattr(session, 'create_client', create)
    monkeypatch.setattr(identity, 'Session', lambda: session)
    yield created
    for stub in stubs:
        stub.deactivate()


def test_actual_sdk_uses_same_explicit_temporary_snapshot(sdk):
    with verified_client(ROLE, CREDS, 'secretsmanager', 'ap-northeast-2', clock=lambda: NOW) as (client, proof):
        assert client.meta.endpoint_url == 'https://secretsmanager.ap-northeast-2.amazonaws.com'
        assert proof['verified_at'] == NOW.isoformat()
        assert proof['expires_at'] == CREDS.expires_at.isoformat()
        assert ROLE.arn not in json.dumps(proof)
        assert ROLE.role_id not in json.dumps(proof)
    assert [entry[0] for entry in sdk] == ['sts', 'secretsmanager']
    for _, kwargs, client in sdk:
        assert kwargs['aws_access_key_id'] == CREDS.access_key_id
        assert kwargs['aws_secret_access_key'] == CREDS.secret_access_key
        assert kwargs['aws_session_token'] == CREDS.session_token
        assert kwargs['verify'] is True
        assert kwargs['config'].ignore_configured_endpoint_urls
        assert kwargs['config'].proxies == {}
        frozen = client._request_signer._credentials.get_frozen_credentials()
        assert frozen.access_key == CREDS.access_key_id
        assert frozen.token == CREDS.session_token


def test_endpoint_and_ca_environment_overrides_ignored(sdk, monkeypatch):
    monkeypatch.setenv('AWS_ENDPOINT_URL', 'http://127.0.0.1:1')
    monkeypatch.setenv('AWS_ENDPOINT_URL_STS', 'http://127.0.0.1:2')
    monkeypatch.setenv('AWS_ENDPOINT_URL_SECRETS_MANAGER', 'http://127.0.0.1:3')
    monkeypatch.setenv('AWS_CA_BUNDLE', '/nonexistent/untrusted-ca.pem')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:4')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'ambient-must-not-be-used')
    with verified_client(ROLE, CREDS, 'secretsmanager', 'ap-northeast-2', clock=lambda: NOW):
        pass
    for _, kwargs, client in sdk:
        assert client.meta.endpoint_url.startswith('https://')
        assert client.meta.endpoint_url.endswith('.amazonaws.com')
        assert kwargs['verify'] is True
        assert kwargs['config'].proxies == {}


def test_anonymous_sdk_client_does_not_resolve_ambient_credentials(sdk, monkeypatch):
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', CANARY)
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', CANARY)
    with anonymous_s3_client('ap-northeast-2') as client:
        assert client.meta.config.signature_version is UNSIGNED
        assert client._request_signer._credentials is None
    assert len(sdk) == 1


@pytest.mark.parametrize('changes', [
    {'Account': '111122223333'},
    {'Arn': 'arn:aws:iam::123456789012:user/fixture-app'},
    {'Arn': 'arn:aws:sts::123456789012:assumed-role/other/fixture-session'},
    {'Arn': 'arn:aws:sts::123456789012:assumed-role/fixture-app/fixture-session/extra'},
    {'UserId': 'AROA' + 'B' * 17 + ':fixture-session'},
    {'UserId': ROLE.role_id + ':different-session'},
    {'ResponseMetadata': {'HTTPStatusCode': 403}},
    {'ResponseMetadata': None},
])
def test_account_role_session_recreation_and_bad_response_block(changes):
    with pytest.raises(IdentityUnavailable, match='AWS_IDENTITY_MISMATCH'):
        validate_identity(ROLE, {**RESPONSE, **changes})


def test_returned_identity_does_not_echo_response_fields():
    evidence = validate_identity(ROLE, {**RESPONSE, 'Unexpected': CANARY})
    assert set(evidence) == {'role_arn_sha256', 'role_id_sha256', 'session_identity_sha256'}
    assert CANARY not in json.dumps(evidence)


@pytest.mark.parametrize('changes', [
    {'arn': 'arn:aws:iam::123456789012:user/fixture-app'},
    {'arn': 'arn:aws:iam::123456789012:role/apps//fixture-app'},
    {'role_id': 'fixture-role'},
    {'role_id': None},
])
def test_role_registration_requires_iam_unique_id(changes):
    with pytest.raises(IdentityUnavailable):
        replace(ROLE, **changes)


@pytest.mark.parametrize('changes', [
    {'access_key_id': 'AKIA' + 'A' * 16}, {'session_token': ''},
    {'secret_access_key': ''}, {'expires_at': NOW.replace(tzinfo=None)},
])
def test_temporary_credentials_required(changes):
    with pytest.raises(IdentityUnavailable):
        replace(CREDS, **changes)


def test_credential_repr_is_redacted():
    assert CANARY not in repr(CREDS)
    assert CREDS.access_key_id not in repr(CREDS)


def test_near_expiry_blocks_before_network(sdk):
    with pytest.raises(IdentityUnavailable, match='TOO_CLOSE_TO_EXPIRY'), verified_client(
        ROLE, replace(CREDS, expires_at=NOW + timedelta(seconds=59)), 's3', 'ap-northeast-2', clock=lambda: NOW
    ):
        pass
    assert not sdk


@pytest.mark.parametrize('checked', [NOW - timedelta(seconds=1), CREDS.expires_at])
def test_identity_clock_and_expiry_rechecked_after_sts(sdk, checked):
    times = iter([NOW, checked])
    with pytest.raises(IdentityUnavailable, match='EXPIRED_OR_CLOCK_CHANGED'), verified_client(
        ROLE, CREDS, 's3', 'ap-northeast-2', clock=lambda: next(times)
    ):
        pass
    assert [entry[0] for entry in sdk] == ['sts']


def test_other_partition_blocked_before_network(sdk):
    with pytest.raises(IdentityUnavailable, match='PARTITION_MISMATCH'), verified_client(
        ROLE, CREDS, 's3', 'cn-north-1', clock=lambda: NOW
    ):
        pass
    assert not sdk


def test_identity_failure_redacts_and_closes_client(monkeypatch):
    sts = Mock()
    sts.get_caller_identity.side_effect = ClientError(
        {'Error': {'Code': 'InvalidClientTokenId', 'Message': CANARY}}, 'GetCallerIdentity')
    session = Mock()
    session.get_partition_for_region.return_value = 'aws'
    session.create_client.return_value = sts
    monkeypatch.setattr(identity, 'Session', lambda: session)
    with pytest.raises(IdentityUnavailable) as error, verified_client(
        ROLE, CREDS, 's3', 'ap-northeast-2', clock=lambda: NOW
    ):
        pass
    assert str(error.value) == 'AWS_IDENTITY_UNAVAILABLE'
    assert error.value.__suppress_context__
    sts.close.assert_called_once()
    assert session.create_client.call_count == 1


def test_debug_logging_blocks_before_credentials_reach_sdk(sdk):
    logger = logging.getLogger('botocore.parsers')
    previous = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        with pytest.raises(IdentityUnavailable, match='WIRE_DEBUG'), verified_client(
            ROLE, CREDS, 's3', 'ap-northeast-2', clock=lambda: NOW
        ):
            pass
        assert not sdk
    finally:
        logger.setLevel(previous)
