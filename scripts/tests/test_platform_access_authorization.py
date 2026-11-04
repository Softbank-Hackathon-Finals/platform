"""Baseline access must query named Pod exec permissions, including opt-in grants."""
import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.tests.test_platform_data import load


class ExecAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.access = load('verify_access')
        self.pods = [{'metadata': {'name': 'app-fixture'}}, {'metadata': {'name': 'pooler-fixture'}}]

    def test_checks_both_verbs_for_all_actual_pods_without_exec(self):
        with patch.object(self.access, 'call', return_value=subprocess.CompletedProcess([], 1, 'no\n', '')) as call:
            result = self.access.exec_denial({}, 'source-fixture', self.pods)
        self.assertTrue(result['denied'])
        queried = {(c.args[3], c.args[4]) for c in call.call_args_list}
        self.assertEqual(queried, {(verb, 'pods/' + pod) for verb in ('get', 'create') for pod in ('app-fixture', 'pooler-fixture')})
        for c in call.call_args_list:
            self.assertEqual(c.args[1:3], ('auth', 'can-i'))
            self.assertEqual(c.args[5:], ('--subresource=exec', '-n', 'source-fixture'))
        self.assertNotIn('app-fixture', json.dumps(result))
        self.assertNotIn('pooler-fixture', json.dumps(result))

    def test_named_opt_in_grant_cannot_be_mistaken_for_denial(self):
        for allowed_verb in ('get', 'create'):
            def call(config, *args, allowed_verb=allowed_verb):
                allowed = args[2:5] == (allowed_verb, 'pods/pooler-fixture', '--subresource=exec')
                return subprocess.CompletedProcess([], 0 if allowed else 1, 'yes' if allowed else 'no', '')
            with self.subTest(allowed_verb=allowed_verb), patch.object(self.access, 'call', call):
                self.assertFalse(self.access.exec_denial({}, 'source-fixture', self.pods)['denied'])

    def test_absent_pods_do_not_prove_denial(self):
        with patch.object(self.access, 'call') as call:
            self.assertIsNone(self.access.exec_denial({}, 'source-fixture', [])['denied'])
        call.assert_not_called()

    def test_api_failure_and_inconsistent_answers_are_unknown(self):
        for rc, out, err in [(1, '', 'Forbidden'), (2, 'no', 'error'), (0, 'no', ''), (1, 'yes', ''), (0, '', '')]:
            with self.subTest(rc=rc, out=out), patch.object(self.access, 'call', return_value=subprocess.CompletedProcess([], rc, out, err)):
                self.assertIsNone(self.access.exec_denial({}, 'source-fixture', self.pods)['denied'])

    def test_transport_failure_is_unknown(self):
        for error in (OSError('unavailable'), subprocess.TimeoutExpired('kubectl', 30)):
            with patch.object(self.access, 'call', side_effect=error):
                self.assertIsNone(self.access.exec_denial({}, 'source-fixture', self.pods)['denied'])

    def test_known_allow_is_failure_even_if_another_query_is_unknown(self):
        replies = [subprocess.CompletedProcess([], 2, '', ''), subprocess.CompletedProcess([], 0, 'yes', '')]
        with patch.object(self.access, 'call', side_effect=replies):
            self.assertFalse(self.access.exec_denial({}, 'source-fixture', self.pods[:1])['denied'])

    def verify_report(self, pods, reply):
        admin = {'contexts': [{'context': {'cluster': 'fixture'}}]}
        cm = json.dumps({'metadata': {'annotations': {}}, 'data': {'mode': 'preserved'}})
        def success(config, *args):
            if args[0:2] == ('create', 'token'):
                return 'opaque-fixture'
            if args[0:2] == ('get', 'pods'):
                return json.dumps({'items': pods})
            return cm
        def call(config, *args):
            if args[0:2] == ('auth', 'can-i'):
                return reply
            return subprocess.CompletedProcess([], 1, '', 'Forbidden')
        with patch.object(self.access, 'success', success), patch.object(self.access, 'call', call):
            return self.access.verify(admin, 'source-fixture', 'pooler-fixture')

    def test_empty_workload_remains_unknown_in_full_report(self):
        result = self.verify_report([], subprocess.CompletedProcess([], 1, 'no', ''))
        self.assertEqual(result['status'], 'UNKNOWN')
        for role in ('collector', 'executor'):
            self.assertIsNone(result['checks'][role + '_can_read_workload'])
            self.assertIsNone(result['checks'][role + '_denied_exec'])
            self.assertEqual(result['exec_authorization'][role]['observations'], [])

    def test_full_report_retains_unknown_and_fails_on_opt_in_allow(self):
        for reply, status, denied in [(subprocess.CompletedProcess([], 0, 'yes', ''), 'FAIL', False),
                                      (subprocess.CompletedProcess([], 2, '', ''), 'UNKNOWN', None),
                                      (subprocess.CompletedProcess([], 1, 'no', ''), 'PASS', True)]:
            with self.subTest(status=status):
                result = self.verify_report(self.pods, reply)
            self.assertEqual(result['status'], status)
            for role in ('collector', 'executor'):
                self.assertIs(result['checks'][role + '_denied_exec'], denied)
                self.assertEqual(len(result['exec_authorization'][role]['observations']), 4)


if __name__ == '__main__':
    unittest.main()
