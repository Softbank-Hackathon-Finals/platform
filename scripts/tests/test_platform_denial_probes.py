"""Denied access must not be inferred from a broken endpoint or broken controls."""
import contextlib
import copy
import io
import json
import runpy
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.tests.test_platform_data import load


class NetworkDenialTests(unittest.TestCase):
    def setUp(self):
        self.probe = load('verify_platform_host')
        self.observations = {
            name: {'outcome': outcome, 'started_at_ms': i * 1000, 'finished_at_ms': i * 1000 + 100, 'duration_ms': 100}
            for i, (name, outcome) in enumerate([('target_before', 'connected'), ('subject_dns', 'resolved'),
                ('subject_service', 'connected'), ('attempt', 'timeout'), ('target_after', 'connected')])}

    def test_transport_timeout_is_an_observation_not_a_pass(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'network_probe.py'
            script.write_text(self.probe.NETWORK_PROBE)
            with patch.object(socket, 'create_connection', side_effect=TimeoutError), patch('sys.argv', ['probe', 'tcp', '192.0.2.1', '5432']), contextlib.redirect_stdout(stream):
                runpy.run_path(str(script))
        result = json.loads(stream.getvalue())
        self.assertEqual(result['outcome'], 'timeout')
        self.assertNotIn('database_network_denied', result)
        self.assertNotIn('verdict', result)

    def test_bounded_controls_qualify_timeout_and_refusal(self):
        for outcome in ('timeout', 'refused'):
            self.observations['attempt']['outcome'] = outcome
            self.assertEqual(self.probe.network_verdict(self.observations, True), 'PASS')

    def test_successful_forbidden_connection_fails(self):
        self.observations['attempt']['outcome'] = 'connected'
        self.assertEqual(self.probe.network_verdict(self.observations, True), 'FAIL')

    def test_db_down_or_any_failed_or_missing_control_is_unknown(self):
        for name in ('target_before', 'target_after', 'subject_dns', 'subject_service'):
            for attempt in ('timeout', 'connected'):
                with self.subTest(name=name, attempt=attempt):
                    rows = copy.deepcopy(self.observations)
                    rows['attempt']['outcome'] = attempt
                    rows[name]['outcome'] = 'unreachable'
                    self.assertEqual(self.probe.network_verdict(rows, True), 'UNKNOWN')
                    del rows[name]
                    self.assertEqual(self.probe.network_verdict(rows, True), 'UNKNOWN')

    def test_wrong_dns_and_unclassified_attempt_are_unknown(self):
        for name, outcome in [('subject_dns', 'unexpected_address'), ('attempt', 'unreachable')]:
            rows = copy.deepcopy(self.observations)
            rows[name]['outcome'] = outcome
            self.assertEqual(self.probe.network_verdict(rows, True), 'UNKNOWN')

    def test_changed_environment_expired_or_unordered_window_is_unknown(self):
        self.assertEqual(self.probe.network_verdict(self.observations, False), 'UNKNOWN')
        for start, finish in [(61000, 62000), (3000, 3100), (5000, 4900), (None, 5500)]:
            rows = copy.deepcopy(self.observations)
            rows['target_after'].update(started_at_ms=start, finished_at_ms=finish)
            self.assertEqual(self.probe.network_verdict(rows, True), 'UNKNOWN')

    def test_unknown_never_becomes_boolean_pass(self):
        self.assertEqual(self.probe.overall_status({'rbac': True, 'network': None}), 'UNKNOWN')
        self.assertEqual(self.probe.overall_status({'rbac': False, 'network': None}), 'FAIL')

    def test_failed_or_unknown_report_exits_unsuccessfully(self):
        config = {'contexts': [{'context': {'cluster': 'fixture'}}]}
        for denied, network_verdict, expected in [(False, 'PASS', 'FAIL'), (True, 'UNKNOWN', 'UNKNOWN')]:
            def admin(*args):
                return json.dumps(config) if args[0] == 'config' else 'opaque-test-token'
            result = types.SimpleNamespace(returncode=1 if denied else 0, stderr='Forbidden' if denied else '')
            network = {namespace: {'verdict': network_verdict} for namespace in ('platform-collector', 'platform-executor')}
            output = io.StringIO()
            with patch.object(self.probe, 'admin', admin), patch.object(self.probe.subprocess, 'run', return_value=result), patch.object(self.probe, 'verify_network', return_value=network), contextlib.redirect_stdout(output):
                self.assertEqual(self.probe.main(), 1)
            self.assertEqual(json.loads(output.getvalue())['status'], expected)

    def test_control_policy_never_grants_subject_database_access_or_credentials(self):
        resources = self.probe.network_resources('t7-network-fixture')
        jobs = [r for r in resources if r['kind'] == 'Job']
        self.assertEqual(len(jobs), 3)
        for job in jobs:
            pod = job['spec']['template']
            self.assertFalse(pod['spec']['automountServiceAccountToken'])
            self.assertNotIn('volumes', pod['spec'])
            if job['metadata']['namespace'] != 'platform-state':
                self.assertNotIn('bravo.dev/db-client', pod['metadata']['labels'])
        for policy in [r for r in resources if r['kind'] == 'NetworkPolicy']:
            self.assertEqual(policy['spec']['podSelector']['matchLabels'], {'bravo.dev/network-probe': 't7-network-fixture'})
            rules = policy['spec'].get('egress', policy['spec'].get('ingress'))
            self.assertTrue({p['port'] for rule in rules for p in rule['ports']} <= {53, 8080})
            self.assertNotIn('5432', json.dumps(policy))


class ConnectionError(Exception):
    pass


class TLSRejectionTests(unittest.TestCase):
    rejection = ConnectionError('connection to server failed: FATAL:  pg_hba.conf rejects connection for host "192.0.2.4", user "platform_runtime", database "platform", no encryption')

    def setUp(self):
        self.probe = load('tls_verification')

    def connection(self, address='192.0.2.10', generation='stable', tls=True):
        connection = Mock()
        connection.info = types.SimpleNamespace(hostaddr=address, port=5432)
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.execute.return_value.fetchone.return_value = (tls, 'TLSv1.3', 'platform_runtime', 'platform', generation)
        return connection

    def run_attempt(self, attempt, before=None, after=None):
        connect = Mock(side_effect=[before or self.connection(), attempt, after or self.connection()])
        result = self.probe.verify_plaintext_rejection('opaque-credentials', connect, ConnectionError)
        return result, connect

    def test_explicit_hba_rejection_between_tls_controls_passes_and_pins_endpoint(self):
        result, connect = self.run_attempt(self.rejection)
        self.assertEqual(result['verdict'], 'PASS')
        calls = connect.call_args_list
        self.assertEqual([c.kwargs['sslmode'] for c in calls], ['verify-full', 'disable', 'verify-full'])
        for call in calls[1:]:
            self.assertEqual(call.args, ('opaque-credentials',))
            self.assertEqual(call.kwargs['hostaddr'], '192.0.2.10')
            self.assertEqual(call.kwargs['port'], 5432)
            self.assertEqual(call.kwargs['gssencmode'], 'disable')
        self.assertNotIn('opaque-credentials', json.dumps(result))
        self.assertNotIn('192.0.2.10', json.dumps(result))

    def test_plaintext_connection_success_fails(self):
        result, _ = self.run_attempt(self.connection())
        self.assertEqual(result['verdict'], 'FAIL')

    def test_transport_authentication_and_wrong_hba_responses_are_unknown(self):
        for message in ['server closed the connection unexpectedly', 'connection timeout expired',
                        'connection refused', 'FATAL: password authentication failed for user "platform_runtime"',
                        str(self.rejection).replace('no encryption', 'SSL encryption'),
                        str(self.rejection).replace('platform_runtime', 'another_user'),
                        str(self.rejection).replace('database "platform"', 'database "other"'),
                        str(self.rejection) + '\nFATAL: another failure']:
            with self.subTest(message=message):
                result, _ = self.run_attempt(ConnectionError(message))
                self.assertEqual(result['verdict'], 'UNKNOWN')
                self.assertNotIn(message, json.dumps(result))

    def test_before_control_failure_does_not_attempt_plaintext(self):
        result, connect = self.run_attempt(self.rejection, before=ConnectionError('DB down'))
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(connect.call_count, 1)
        self.assertNotIn('plaintext', result['observations'])

    def test_after_control_failure_or_changed_server_is_unknown(self):
        for after in (ConnectionError('DB down'), self.connection(address='192.0.2.20'),
                      self.connection(generation='restarted'), self.connection(tls=False)):
            result, connect = self.run_attempt(self.rejection, after=after)
            self.assertEqual(result['verdict'], 'UNKNOWN')
            self.assertEqual(connect.call_count, 3)

    def test_expired_controls_are_unknown(self):
        with patch.object(self.probe.time, 'monotonic', side_effect=[0, 61]):
            result, _ = self.run_attempt(self.rejection)
        self.assertEqual(result['verdict'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
