import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.session import Session
from botocore.stub import Stubber

from bravo_controls import aws_identity
from bravo_controls.aws_identity import IdentityUnavailable, RegisteredRole, TemporaryCredentials
from bravo_controls.aws_probes import ObjectTarget, SecretTarget
from bravo_controls.aws_qualification import QualifiedAwsProbe
from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.runner import TestSpec as ControlSpec

NOW = datetime(2026, 11, 4, tzinfo=UTC)
ENV = Environment('source', 'source', 'registered/source')
CONTEXT = RunContext('test-run', 'demo-chat', ENV)
SPEC = ControlSpec('SEC-01.1', 'SEC-01', '1', 'deny', 'required')
APP = RegisteredRole('arn:aws:iam::123456789012:role/apps/fixture-app', 'AROA' + 'A' * 17)
OTHER = RegisteredRole('arn:aws:iam::123456789012:role/apps/fixture-other', 'AROA' + 'B' * 17)
CANARY = 'do-not-retain-credential-or-secret-canary'
TARGET = SecretTarget('demo-chat', ENV, 'arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:fixture-abcdef', 'a' * 32)
CREDENTIALS = {
    APP: TemporaryCredentials('ASIA' + 'A' * 16, CANARY, CANARY, NOW + timedelta(minutes=15)),
    OTHER: TemporaryCredentials('ASIA' + 'B' * 16, CANARY, CANARY, NOW + timedelta(minutes=15)),
}


@pytest.fixture
def transport(monkeypatch):
    clients, stubs, calls = [], [], []
    state = {'other_outcome': 'deny', 'wrong_identity': False}

    def session():
        sdk = Session()
        create = sdk.create_client

        def client(service, **kwargs):
            role = APP if kwargs['aws_access_key_id'] == CREDENTIALS[APP].access_key_id else OTHER
            conn = create(service, **kwargs)
            clients.append(conn)
            calls.append((service, role))
            stub = Stubber(conn)
            if service == 'sts':
                actual = APP if state['wrong_identity'] else role
                name = actual.arn.rsplit('/', 1)[-1]
                stub.add_response('get_caller_identity', {
                    'Account': '123456789012',
                    'Arn': f'arn:aws:sts::123456789012:assumed-role/{name}/test-session',
                    'UserId': actual.role_id + ':test-session',
                    'ResponseMetadata': {'HTTPStatusCode': 200},
                })
            elif service == 'secretsmanager':
                request = {'SecretId': TARGET.secret_arn, 'VersionId': TARGET.version_id, 'VersionStage': 'AWSCURRENT'}
                if role == APP or state['other_outcome'] == 'allow':
                    stub.add_response('get_secret_value', {
                        'ARN': TARGET.secret_arn, 'VersionId': TARGET.version_id,
                        'VersionStages': ['AWSCURRENT'], 'SecretString': CANARY,
                        'ResponseMetadata': {'HTTPStatusCode': 200},
                    }, request)
                elif state['other_outcome'] == 'expired':
                    stub.add_client_error('get_secret_value', service_error_code='ExpiredTokenException',
                                          service_message=CANARY, http_status_code=400, expected_params=request)
                else:
                    stub.add_client_error('get_secret_value', service_error_code='AccessDeniedException',
                                          service_message=CANARY, http_status_code=400, expected_params=request)
            elif service == 's3':
                request = {'Bucket': 'fixture-bucket', 'Key': 'fixture/object', 'ExpectedBucketOwner': '123456789012'}
                if kwargs['aws_access_key_id']:
                    for _ in range(2):
                        stub.add_response('get_object', {'VersionId': 'version-1', 'Body': BytesIO(b'not-read'),
                                                        'ResponseMetadata': {'HTTPStatusCode': 200}}, request)
                else:
                    stub.add_client_error('get_object', service_error_code='AccessDenied',
                                          service_message=CANARY, http_status_code=403, expected_params=request)
            stub.activate()
            stubs.append(stub)
            return conn

        sdk.create_client = client
        return sdk

    monkeypatch.setattr(aws_identity, 'Session', session)
    state['calls'] = calls
    yield state
    for stub in stubs:
        stub.deactivate()
    for conn in clients:
        conn.close()


def probe(sink, **changes):
    values = dict(app_role=APP, other_role=OTHER, credential_provider=CREDENTIALS.__getitem__,
                  evidence_sink=sink, clock=lambda: NOW)
    values.update(changes)
    return QualifiedAwsProbe(TARGET, **values)


def test_probe_checks_both_real_sdk_identities_then_retains_bound_evidence(transport):
    records = []
    result = probe(records.append)(CONTEXT, SPEC)
    assert result.observed == 'deny' and result.positive_control == 'pass'
    assert result.unknown_reason is None
    assert result.output_sha256 == 'sha256:' + hashlib.sha256(records[0]).hexdigest()
    assert len(records) == 1
    doc = json.loads(records[0])
    assert len(doc['identities']) == 2
    assert doc['run_id'] == CONTEXT.run_id
    assert doc['test_id'] == SPEC.test_id
    assert doc['probe']['run_id'] == CONTEXT.run_id
    assert doc['probe_sha256'] == 'sha256:' + hashlib.sha256(
        json.dumps(doc['probe'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert CANARY not in records[0].decode()
    assert TARGET.secret_arn not in records[0].decode()
    assert APP.role_id not in records[0].decode()
    assert transport['calls'] == [('sts', APP), ('secretsmanager', APP), ('sts', OTHER), ('secretsmanager', OTHER)]


def test_alias_credentials_cannot_fake_another_role(transport):
    transport['wrong_identity'] = True
    sink = Mock()
    with pytest.raises(IdentityUnavailable, match='MISMATCH'):
        probe(sink)(CONTEXT, SPEC)
    sink.assert_not_called()
    assert transport['calls'][-1] == ('sts', OTHER)


@pytest.mark.parametrize('outcome,observed,reason', [('allow', 'allow', None), ('expired', None, 'TOOL_ERROR')])
def test_real_sdk_denial_is_not_confused_with_other_outcomes(transport, outcome, observed, reason):
    transport['other_outcome'] = outcome
    records = []
    result = probe(records.append)(CONTEXT, SPEC)
    assert result.observed == observed
    assert result.unknown_reason == reason
    assert CANARY not in records[0].decode()


def test_retention_failure_cannot_return_success(transport):
    sink = Mock(side_effect=RuntimeError(CANARY))
    with pytest.raises(IdentityUnavailable, match='RETENTION_FAILED') as error:
        probe(sink)(CONTEXT, SPEC)
    assert CANARY not in str(error.value)
    assert error.value.__suppress_context__


def test_provider_failure_does_not_leak(transport):
    provider = Mock(side_effect=RuntimeError(CANARY))
    with pytest.raises(IdentityUnavailable, match='PROVIDER_UNAVAILABLE') as error:
        probe(Mock(), credential_provider=provider)(CONTEXT, SPEC)
    assert CANARY not in str(error.value)
    assert not transport['calls']


@pytest.mark.parametrize('context,spec', [
    (replace(CONTEXT, app='other-app'), SPEC),
    (replace(CONTEXT, environment=replace(ENV, ref='other/environment')), SPEC),
    (CONTEXT, replace(SPEC, test_id='SEC-01.2')),
    (CONTEXT, replace(SPEC, method_version='2')),
    (CONTEXT, replace(SPEC, expected='allow')),
])
def test_scope_is_checked_before_credentials(transport, context, spec):
    provider = Mock()
    with pytest.raises(IdentityUnavailable, match='CONTEXT_MISMATCH'):
        probe(Mock(), credential_provider=provider)(context, spec)
    provider.assert_not_called()
    assert not transport['calls']


def test_misregistered_role_account_rejected_before_runtime():
    other_account = RegisteredRole(APP.arn.replace('123456789012', '111122223333'), APP.role_id)
    with pytest.raises(IdentityUnavailable, match='ROLE_TARGET_MISMATCH'):
        probe(Mock(), app_role=other_account)


def test_missing_qualification_does_not_disappear_from_full_runner(transport):
    root = Path(__file__).resolve().parents[3] / 'docs/controls'
    catalog = Catalog(json.loads((root / 'profiles.json').read_text()),
                      json.loads((root / 'control-result.schema.json').read_text()))
    runner = Runner(catalog, {'SEC-01.1': probe(Mock(), credential_provider=Mock(side_effect=RuntimeError(CANARY)))},
                    clock=lambda: NOW, evidence_ttl=timedelta(minutes=5))
    run = runner.run(profile_id='full', profile_version='1', app='demo-chat', environment=ENV)
    assert len(run['results']) == 20
    assert all(row['verdict'] == 'UNKNOWN' for row in run['results'])
    assert CANARY not in json.dumps(run)


def test_s3_qualified_path_preserves_anonymous_get_and_both_positive_controls(transport):
    target = ObjectTarget('demo-chat', ENV, '123456789012', 'ap-northeast-2',
                          'fixture-bucket', 'fixture/object', 'version-1')
    records = []
    check = QualifiedAwsProbe(target, app_role=APP, credential_provider=CREDENTIALS.__getitem__,
                              evidence_sink=records.append, clock=lambda: NOW)
    result = check(CONTEXT, ControlSpec('IAM-01.1', 'IAM-01', '1', 'deny', 'required'))
    assert result.observed == 'deny' and result.positive_control == 'pass'
    document = json.loads(records[0])
    assert len(document['identities']) == 1
    assert document['probe']['positive']['outcome'] == 'allow'
    assert document['probe']['negative']['outcome'] == 'deny'
    assert document['probe']['postpositive']['outcome'] == 'allow'
    assert CANARY not in records[0].decode()
