"""Do not confuse frontend TLS, stale sessions, or a config file with ENC-01.5."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.pooler_backend_tls import SPEC, BackendTarget, PoolerBackendTLSProbe
from bravo_controls.runner import Environment, RunContext

START = datetime(2026, 11, 3, 12, tzinfo=UTC)
ENV = Environment('backend-local', 'rehearsal', 'local:owned')
TARGET = BackendTarget('demo-chat', ENV, 'pool-uid', 'pod-uid', 'db-uid', 'chat',
                       '10.1.0.2', '10.1.0.3', 'sha256:' + 'a' * 64)
CONTEXT = RunContext('run-one', 'demo-chat', ENV)


def fixture():
    return {
        'run_id': CONTEXT.run_id,
        'before': {'binding': TARGET.binding(), 'collected_at': START + timedelta(seconds=1),
                   'server_tls_sslmode': 'verify-full'},
        'after': {'binding': TARGET.binding(), 'collected_at': START + timedelta(seconds=4),
                  'server_tls_sslmode': 'verify-full'},
        'connection': {'collected_at': START + timedelta(seconds=3),
                       'backend_started_at': START + timedelta(seconds=2), 'database': 'chat',
                       'client_address': '10.1.0.2', 'server_address': '10.1.0.3',
                       'ssl': True, 'protocol': 'TLSv1.3', 'pid': 42},
    }


def invoke(report=None, *, collector=None, sink=None, finish=5, context=CONTEXT, spec=SPEC):
    clock = iter([START, START + timedelta(seconds=finish)])
    evidence = []
    probe = PoolerBackendTLSProbe(TARGET, collector=collector or (lambda *_: report or fixture()),
                                 evidence_sink=sink or evidence.append, clock=lambda: next(clock))
    return probe(context, spec), evidence


def test_valid_runtime_and_fresh_actual_backend():
    result, evidence = invoke()
    assert result.observed == 'enabled' and result.unknown_reason is None
    document = json.loads(evidence[0])
    assert document['facts']['fresh_backend'] and document['facts']['binding_unchanged']
    assert '10.1.0.' not in evidence[0].decode() and 'pod-uid' not in evidence[0].decode()
    assert 'password' not in evidence[0].decode()


@pytest.mark.parametrize('mode', ['disable', 'allow', 'prefer', 'require', 'verify-ca'])
def test_encrypted_session_without_hostname_validation_fails(mode):
    report = fixture()
    report['before']['server_tls_sslmode'] = report['after']['server_tls_sslmode'] = mode
    result, _ = invoke(report)
    assert result.observed == 'disabled' and result.unknown_reason is None


@pytest.mark.parametrize('ssl,protocol', [(False, None), (True, 'TLSv1'), (True, 'TLSv1.1')])
def test_plaintext_or_old_tls_fails_even_with_full_config(ssl, protocol):
    report = fixture()
    report['connection'].update(ssl=ssl, protocol=protocol)
    result, _ = invoke(report)
    assert result.observed == 'disabled'


@pytest.mark.parametrize('section,key,value', [
    ('connection', 'database', 'other-app'),
    ('connection', 'client_address', '10.1.0.99'),
    ('connection', 'server_address', '10.1.0.99'),
    ('connection', 'client_address', 'hostname.invalid'),
    ('connection', 'ssl', 'true'),
    ('connection', 'ssl', 1),
    ('connection', 'pid', True),
    ('connection', 'pid', 0),
    ('connection', 'protocol', 'TLSv9'),
    ('connection', 'protocol', None),
    ('connection', 'backend_started_at', START),
    ('connection', 'backend_started_at', START + timedelta(seconds=4)),
    ('connection', 'backend_started_at', START.replace(tzinfo=None)),
    ('connection', 'collected_at', START + timedelta(seconds=5)),
    ('before', 'collected_at', START - timedelta(seconds=1)),
    ('after', 'collected_at', START + timedelta(seconds=6)),
    ('after', 'server_tls_sslmode', 'verify-ca'),
    ('before', 'server_tls_sslmode', 'unknown-mode'),
])
def test_invalid_readback_never_yields_pass(section, key, value):
    report = fixture()
    report[section][key] = value
    result, _ = invoke(report)
    assert result.observed is None and result.unknown_reason == 'UNEXPECTED_RESPONSE'


@pytest.mark.parametrize('section', ['before', 'after'])
@pytest.mark.parametrize('key', list(TARGET.binding()))
def test_every_resource_binding_is_checked_at_both_ends(section, key):
    report = fixture()
    report[section]['binding'][key] = 'other-resource'
    result, _ = invoke(report)
    assert result.observed is None


@pytest.mark.parametrize('section', ['before', 'after', 'connection'])
def test_missing_or_ambiguous_readback(section):
    report = fixture()
    del report[section]
    assert invoke(report)[0].observed is None
    report = fixture()
    report[section]['untrusted_extra'] = 'do not log this'
    result, evidence = invoke(report)
    assert result.observed is None and b'do not log this' not in evidence[0]


def test_wrong_invocation_and_timeout_are_unknown():
    report = fixture()
    report['run_id'] = 'old-run'
    assert invoke(report)[0].observed is None
    assert invoke(finish=31)[0].observed is None


@pytest.mark.parametrize('error,reason', [(TimeoutError, 'TIMEOUT'), (PermissionError, 'OBSERVATION_DENIED'),
                                         (OSError, 'TOOL_ERROR'), (ValueError, 'TOOL_ERROR')])
def test_collection_errors_do_not_become_positive_evidence(error, reason):
    def collect(*_):
        raise error('credential-bearing-diagnostic')
    result, evidence = invoke(collector=collect)
    assert result.observed is None and result.unknown_reason == reason
    assert b'credential-bearing-diagnostic' not in evidence[0]


def test_durable_evidence_failure_does_not_yield_observation():
    def sink(_):
        raise OSError('unavailable')
    with pytest.raises(OSError):
        invoke(sink=sink)


@pytest.mark.parametrize('context,spec', [
    (replace(CONTEXT, app='other'), SPEC),
    (replace(CONTEXT, environment=replace(ENV, id='other')), SPEC),
    (replace(CONTEXT, run_id='line\nbreak'), SPEC),
    (CONTEXT, replace(SPEC, test_id='ENC-01.1')),
    (CONTEXT, replace(SPEC, expected='disabled')),
])
def test_scope_must_match(context, spec):
    with pytest.raises(ValueError):
        invoke(context=context, spec=spec)


@pytest.mark.parametrize('key,value', [('pooler_address', 'host.invalid'), ('database_address', 42),
                                     ('registration_sha256', 'sha256:broken'), ('database_uid', '')])
def test_registration_must_be_bounded(key, value):
    with pytest.raises(ValueError):
        replace(TARGET, **{key: value})


def test_provider_report_not_mutated():
    report = fixture()
    original = deepcopy(report)
    invoke(report)
    assert report == original


@pytest.mark.parametrize('mode,verdict', [('verify-full', 'PASS'), ('verify-ca', 'FAIL')])
def test_runner_maps_backend_control_without_claiming_other_tls_hops(mode, verdict):
    from pathlib import Path

    from bravo_controls.runner import Catalog, Runner

    root = Path(__file__).resolve().parents[3]
    catalog = Catalog(json.loads((root / 'docs/controls/profiles.json').read_text()),
                      json.loads((root / 'docs/controls/control-result.schema.json').read_text()))
    def collect(context, target, deadline):
        report = fixture()
        report['run_id'] = context.run_id
        report['before']['server_tls_sslmode'] = report['after']['server_tls_sslmode'] = mode
        return report
    clock = iter([START, START + timedelta(seconds=5)])
    current = [START]
    def probe_clock():
        current[0] = next(clock)
        return current[0]
    probe = PoolerBackendTLSProbe(TARGET, collector=collect, evidence_sink=lambda _: None,
                                 clock=probe_clock)
    run = Runner(catalog, {SPEC.test_id: probe}, clock=lambda: current[0]).run(
        profile_id='full', profile_version='1', app='demo-chat', environment=ENV)
    rows = {x['test_id']: x for x in run['results']}
    assert rows[SPEC.test_id]['verdict'] == verdict
    assert rows['ENC-01.1']['verdict'] == rows['ENC-01.4']['verdict'] == 'UNKNOWN'
