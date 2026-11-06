from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import botocore.session
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from bravo_controls.audit_forwarding import AuditFileRegistration, collector_config
from bravo_controls.cloudwatch_audit import (
    CloudWatchAuditRegistration,
    cloudwatch_collector_config,
    destination_preflight,
    writer_policy,
)

NOW = datetime(2026, 11, 6, 3, tzinfo=UTC)
GROUP = ':'.join(('arn', 'aws', 'logs', 'ap-northeast-2', '123456789012', 'log-group', '/bravo/audit/source'))
KEY = ':'.join(('arn', 'aws', 'kms', 'ap-northeast-2', '123456789012', 'key/11111111-1111-4111-8111-111111111111'))


def registration():
    return CloudWatchAuditRegistration(
        'source', 'source', 'sha256:' + 'a' * 64, 'source-dev', 'source-db-1',
        '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
        GROUP, KEY, 7,
    )


def clients():
    session = botocore.session.get_session()
    cfg = Config(signature_version='v4', connect_timeout=2, read_timeout=3,
                 retries={'mode': 'standard', 'total_max_attempts': 2})
    return [session.create_client(service, region_name='ap-northeast-2', config=cfg,
                                  aws_access_key_id='fixture', aws_secret_access_key='fixture')
            for service in ('logs', 'kms')]


def responses():
    b = registration()
    return [
        {'logGroups': [{'logGroupName': b.group, 'arn': GROUP + ':*',
                        'kmsKeyId': KEY, 'retentionInDays': 7, 'creationTime': 1000,
                        'logGroupClass': 'STANDARD'}], 'ResponseMetadata': {'HTTPStatusCode': 200}},
        {'logStreams': [{'logStreamName': b.stream, 'arn': GROUP + ':log-stream:' + b.stream,
                         'creationTime': 1001}], 'ResponseMetadata': {'HTTPStatusCode': 200}},
        {'KeyMetadata': {'Arn': KEY, 'KeyId': KEY.split('/')[-1], 'KeyState': 'Enabled',
                         'Enabled': True, 'KeyUsage': 'ENCRYPT_DECRYPT',
                         'KeySpec': 'SYMMETRIC_DEFAULT'}, 'ResponseMetadata': {'HTTPStatusCode': 200}},
    ]


def run(response_sets):
    logs, kms = clients()
    b = registration()
    with Stubber(logs) as ls, Stubber(kms) as ks:
        for group, stream, key in response_sets:
            ls.add_response('describe_log_groups', group, {'logGroupNamePrefix': b.group, 'limit': 50})
            ls.add_response('describe_log_streams', stream, {'logGroupName': b.group,
                             'logStreamNamePrefix': b.stream, 'limit': 50})
            ks.add_response('describe_key', key, {'KeyId': KEY})
        return destination_preflight(b, logs=logs, kms=kms, clock=lambda: NOW)


@pytest.mark.parametrize('update', [
    {'environment': '*'}, {'destination_environment': 'target'}, {'pod_uid': 'other'},
    {'registration_digest': 'arbitrary'}, {'namespace': '../../logs'},
    {'log_group_arn': GROUP + ':*'}, {'log_group_arn': GROUP + '{PodName}'},
    {'log_group_arn': GROUP.replace('ap-northeast-2', 'us-east-1')},
    {'kms_key_arn': KEY.replace('123456789012', '111122223333')},
    {'kms_key_arn': KEY.replace('key/', 'alias/')},
    {'retention_days': 0}, {'retention_days': True}, {'retention_days': 2},
])
def test_bad_or_cross_environment_registration_refused(update):
    with pytest.raises(ValueError):
        replace(registration(), **update)


def test_fixed_scope_credential_free_config_and_exact_writer_policy():
    b = registration()
    config = cloudwatch_collector_config(b)
    exporter = config['exporters']['awscloudwatchlogs/audit']
    assert exporter['log_stream_name'] == b.stream
    assert exporter['region'] == b.region
    assert set(config['receivers']) == {'filelog/cnpg'}
    assert set(config['exporters']) == {'awscloudwatchlogs/audit'}
    assert exporter['sending_queue']['storage'] == 'file_storage/audit'
    assert exporter['retry_on_failure']['max_elapsed_time'] == '0s'
    assert not {'endpoint', 'role_arn', 'external_id', 'log_retention', 'tags'} & exporter.keys()
    statement = writer_policy(b)['Statement'][0]
    assert statement['Action'] == ['logs:CreateLogStream', 'logs:PutLogEvents']
    assert statement['Resource'] == GROUP + ':log-stream:' + b.stream
    assert '*' not in statement['Resource']
    statements = config['processors']['transform/cnpg']['log_statements'][0]['statements']
    assert 'keep_keys(cache, ["logger", "record"])' in statements
    assert replace(b, pod_uid='33333333-3333-4333-8333-33333333333a').stream != b.stream


def test_source_type_does_not_bypass_destination_validation():
    b = registration()
    raw = AuditFileRegistration(b.environment, b.destination_environment, b.registration_digest,
                                b.namespace, b.pod, b.pod_uid, b.node_uid)
    for function in (collector_config, cloudwatch_collector_config, writer_policy):
        with pytest.raises(ValueError):
            function(raw)


def test_two_identical_readbacks_are_readiness_only():
    result = run([responses(), responses()])
    assert result['configuration_ready'] is True
    assert result['delivery_verified'] is False
    assert result['gate_eligible'] is False
    assert result['checked_at'] == NOW.isoformat()


@pytest.mark.parametrize('index, mutate', [
    (0, lambda x: x.update(nextToken='more')),
    (1, lambda x: x.update(nextToken='more')),
    (0, lambda x: x['logGroups'].clear()),
    (1, lambda x: x['logStreams'].clear()),
    (0, lambda x: x['logGroups'].append(deepcopy(x['logGroups'][0]))),
    (1, lambda x: x['logStreams'].append(deepcopy(x['logStreams'][0]))),
    (0, lambda x: x['logGroups'][0].pop('kmsKeyId')),
    (0, lambda x: x['logGroups'][0].update(retentionInDays=365)),
    (0, lambda x: x['logGroups'][0].update(logGroupClass='INFREQUENT_ACCESS')),
    (1, lambda x: x['logStreams'][0].update(arn=GROUP + ':log-stream:other')),
    (2, lambda x: x['KeyMetadata'].update(KeyState='PendingDeletion')),
    (2, lambda x: x['KeyMetadata'].update(Enabled=False)),
    (2, lambda x: x['KeyMetadata'].update(KeyUsage='SIGN_VERIFY')),
    (2, lambda x: x['KeyMetadata'].update(KeySpec='RSA_2048')),
    (0, lambda x: x['logGroups'][0].pop('creationTime')),
    (0, lambda x: x['ResponseMetadata'].update(HTTPStatusCode=403)),
])
def test_incomplete_or_weakened_destination_cannot_qualify(index, mutate):
    data = responses()
    mutate(data[index])
    with pytest.raises(ValueError, match='not qualified'):
        run([data])


@pytest.mark.parametrize('index, field', [(0, 'logGroups'), (1, 'logStreams')])
def test_recreated_group_or_stream_rejected(index, field):
    data = responses()
    data[index][field][0]['creationTime'] += 1
    with pytest.raises(ValueError, match='changed'):
        run([responses(), data])


def test_provider_error_is_redacted():
    logs, kms = clients()
    with Stubber(logs) as stub:
        stub.add_client_error('describe_log_groups', service_error_code='AccessDeniedException',
                              service_message='private-provider-diagnostic', http_status_code=400)
        with pytest.raises(ValueError) as result:
            destination_preflight(registration(), logs=logs, kms=kms)
        assert 'private-provider-diagnostic' not in str(result.value)


def test_client_cannot_disable_tls():
    logs, kms = clients()
    logs._endpoint.http_session._verify = False
    with pytest.raises(ValueError, match='verified'):
        destination_preflight(registration(), logs=logs, kms=kms)


def test_client_cannot_redirect_to_custom_endpoint():
    logs, kms = clients()
    logs.meta._endpoint_url = 'http://example.invalid'
    with pytest.raises(ValueError, match='verified'):
        destination_preflight(registration(), logs=logs, kms=kms)


def test_expired_preflight_does_not_issue_network_calls():
    logs, kms = clients()
    ticks = iter((0, 61))
    with Stubber(logs), Stubber(kms), pytest.raises(ValueError, match='not qualified'):
        destination_preflight(registration(), logs=logs, kms=kms, monotonic=lambda: next(ticks))


def test_bad_timezone_not_usable_as_qualification_time():
    logs, kms = clients()
    b = registration()
    with Stubber(logs) as ls, Stubber(kms) as ks:
        for _ in range(2):
            group, stream, key = responses()
            ls.add_response('describe_log_groups', group)
            ls.add_response('describe_log_streams', stream)
            ks.add_response('describe_key', key)
        with pytest.raises(ValueError, match='UTC'):
            destination_preflight(b, logs=logs, kms=kms, clock=lambda: datetime(2026, 11, 6))
