import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from botocore.session import Session
from botocore.stub import Stubber

from bravo_controls import aws_identity
from bravo_controls.audit_probes import CanaryReceipt
from bravo_controls.audit_qualification import QualifiedCloudWatchAuditProbe
from bravo_controls.aws_identity import IdentityUnavailable, RegisteredRole, TemporaryCredentials
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 6, 3, 0, 20, tzinfo=UTC)
CTX = RunContext('audit-run', 'demo-chat', Environment('aws', 'source', 'registered/source'))
SPEC = Spec('AUD-01.1', 'AUD-01', '1', 'present', 'not_applicable')
ARN = ':'.join(('arn', 'aws', 'logs', 'ap-northeast-2', '123456789012', 'log-group', 'example-audit'))
ROLE = RegisteredRole('arn:aws:iam::123456789012:role/fixture-reader', 'AROA'+'A'*17)
SECRET = 'never-persist-fixture-secret'
CREDS = TemporaryCredentials('ASIA'+'A'*16, SECRET, SECRET, NOW+timedelta(minutes=15))
IDENTITY = {'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/fixture-reader/fixture-session',
            'UserId': ROLE.role_id+':fixture-session', 'ResponseMetadata': {'HTTPStatusCode': 200}}
MARKER = 'bravo_audit_'+'a'*32
STREAMS = ('audit-stream',)


def receipt():
    return CanaryReceipt(CTX, SPEC.test_id, MARKER, 'postgres', 'chat', 'source-dev', 'probe',
                         NOW-timedelta(seconds=10), NOW-timedelta(seconds=5),
                         NOW+timedelta(minutes=9), frozenset({'login'}))


def page():
    message = {'logger': 'postgres', 'record': {
        'user_name': 'postgres', 'database_name': 'chat', 'application_name': MARKER,
        'error_severity': 'LOG', 'message': 'connection authorized: user=postgres database=chat application_name='+MARKER}}
    ms = int((NOW-timedelta(seconds=3)).timestamp()*1000)
    return {'events': [{'logStreamName': STREAMS[0], 'eventId': 'event-1',
                        'timestamp': ms, 'ingestionTime': ms+1000, 'message': json.dumps(message)}],
            'ResponseMetadata': {'HTTPStatusCode': 200}}


@pytest.fixture
def sdk(monkeypatch):
    session = Session()
    create = session.create_client
    state = {'identity': IDENTITY, 'page': page(), 'created': [], 'close': [],
             'clock': NOW, 'after_query': None}
    stubs = []

    def client(service, **kwargs):
        value = create(service, **kwargs)
        state['created'].append((service, kwargs))
        close = value.close
        value.close = lambda: (state['close'].append(service), close())[-1]
        stub = Stubber(value)
        if service == 'sts':
            stub.add_response('get_caller_identity', state['identity'])
        elif state.get('error'):
            stub.add_client_error('filter_log_events', service_error_code=state['error'],
                                  service_message=SECRET, http_status_code=403)
        else:
            stub.add_response('filter_log_events', state['page'])
            original = value.filter_log_events

            def query(**kw):
                result = original(**kw)
                if state['after_query']:
                    state['clock'] = state['after_query']
                return result
            value.filter_log_events = query
        stub.activate()
        stubs.append(stub)
        return value

    monkeypatch.setattr(session, 'create_client', client)
    monkeypatch.setattr(aws_identity, 'Session', lambda: session)
    yield state
    for stub in stubs:
        stub.deactivate()


def probe(sdk, sink, **updates):
    values = dict(app=CTX.app, environment=CTX.environment, reader_role=ROLE, log_group_arn=ARN,
                  streams=STREAMS, credential_provider=lambda role: CREDS,
                  receipt_provider=lambda ctx, spec: receipt(), evidence_sink=sink,
                  clock=lambda: sdk['clock'], monotonic=lambda: 0)
    values.update(updates)
    return QualifiedCloudWatchAuditProbe(**values)


def test_identity_and_exact_observation_digest_are_retained(sdk):
    saved = []
    result = probe(sdk, saved.append)(CTX, SPEC)
    assert result.observed == 'present'
    assert result.tool == 'bravo-cloudwatch-audit-qualified'
    assert len(saved) == 1
    data = json.loads(saved[0])
    assert data['kind'] == 'qualified-cloudwatch-audit.v1'
    assert data['reader_identity']['verified_at'] == NOW.isoformat()
    assert data['reader_identity']['expires_at'] == CREDS.expires_at.isoformat()
    raw = json.dumps(data['probe'], sort_keys=True, separators=(',', ':')).encode()
    assert data['probe_sha256'] == 'sha256:'+hashlib.sha256(raw).hexdigest()
    assert result.output_sha256 == 'sha256:'+hashlib.sha256(saved[0]).hexdigest()
    assert SECRET.encode() not in saved[0] and ROLE.arn.encode() not in saved[0]
    assert MARKER.encode() not in saved[0] and b'connection authorized' not in saved[0]
    assert [x[0] for x in sdk['created']] == ['sts', 'logs']
    assert sorted(sdk['close']) == ['logs', 'sts']
    for _, kwargs in sdk['created']:
        assert kwargs['aws_session_token'] == CREDS.session_token
        assert kwargs['verify'] is True
        assert kwargs['config'].ignore_configured_endpoint_urls
        assert kwargs['config'].proxies == {}


@pytest.mark.parametrize('change', [
    {'app': 'other'}, {'environment': Environment('target', 'target', 'registered/target')},
])
def test_context_mismatch_never_resolves_credentials(sdk, change):
    def provider(_):
        pytest.fail('credentials must not be resolved')
    with pytest.raises(IdentityUnavailable, match='CONTEXT'):
        probe(sdk, lambda raw: None, credential_provider=provider)(replace(CTX, **change), SPEC)
    assert not sdk['created']


@pytest.mark.parametrize('changes', [
    {'reader_role': RegisteredRole('arn:aws:iam::111122223333:role/fixture-reader', ROLE.role_id)},
    {'streams': ()}, {'streams': ('duplicate', 'duplicate')}, {'streams': ('wild*',)},
    {'log_group_arn': ARN+':*'}, {'log_group_arn': 'http://untrusted'},
])
def test_registration_mismatch_fails_before_sdk(sdk, changes):
    with pytest.raises(IdentityUnavailable):
        probe(sdk, lambda raw: None, **changes)
    assert not sdk['created']


@pytest.mark.parametrize('change', [
    {'context': replace(CTX, run_id='other-run')}, {'test_id': 'AUD-01.2', 'acknowledged': frozenset({'create', 'delete'})},
    {'completed_at': NOW+timedelta(seconds=1)},
])
def test_wrong_receipt_is_not_queried(sdk, change):
    with pytest.raises(IdentityUnavailable, match='RECEIPT'):
        probe(sdk, lambda raw: None, receipt_provider=lambda ctx, spec: replace(receipt(), **change))(CTX, SPEC)
    assert not sdk['created']


def test_wrong_sts_role_id_never_queries_logs(sdk):
    sdk['identity'] = {**IDENTITY, 'UserId': 'AROA'+'B'*17+':fixture-session'}
    with pytest.raises(IdentityUnavailable, match='IDENTITY_MISMATCH'):
        probe(sdk, lambda raw: None)(CTX, SPEC)
    assert [x[0] for x in sdk['created']] == ['sts']


def test_credentials_expiring_during_search_cannot_pass(sdk):
    sdk['after_query'] = NOW+timedelta(minutes=16)
    with pytest.raises(IdentityUnavailable, match='EXPIRED'):
        probe(sdk, lambda raw: pytest.fail('must not retain qualification'))(CTX, SPEC)


def test_retention_failure_is_not_a_pass_or_provider_diagnostic(sdk):
    def fail(raw):
        raise RuntimeError(SECRET)
    with pytest.raises(IdentityUnavailable) as error:
        probe(sdk, fail)(CTX, SPEC)
    assert str(error.value) == 'AWS_AUDIT_RETENTION_FAILED'
    assert sorted(sdk['close']) == ['logs', 'sts']


def test_clock_change_during_retention_blocks_return(sdk):
    def sink(raw):
        sdk['clock'] = NOW-timedelta(seconds=1)
    with pytest.raises(IdentityUnavailable, match='CLOCK_CHANGED'):
        probe(sdk, sink)(CTX, SPEC)


def test_empty_search_stays_unknown_with_identity(sdk):
    sdk['page'] = {'events': [], 'ResponseMetadata': {'HTTPStatusCode': 200}}
    saved = []
    result = probe(sdk, saved.append)(CTX, SPEC)
    assert result.observed is None and result.unknown_reason == 'NOT_RUN'
    assert json.loads(saved[0])['probe']['found_actions'] == []


def test_permission_denial_does_not_become_absence_or_pass(sdk):
    sdk['error'] = 'AccessDeniedException'
    with pytest.raises(PermissionError) as error:
        probe(sdk, lambda raw: pytest.fail('no completed evidence'))(CTX, SPEC)
    assert SECRET not in str(error.value)


def test_elapsed_budget_cannot_return_observation(sdk):
    ticks = iter((0, 0, 61))
    with pytest.raises(IdentityUnavailable, match='DEADLINE'):
        probe(sdk, lambda raw: pytest.fail('no evidence'), monotonic=lambda: next(ticks))(CTX, SPEC)
