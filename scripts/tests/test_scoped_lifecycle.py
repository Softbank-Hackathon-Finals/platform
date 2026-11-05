"""Exercise the host boundary with T15's actual namespace lifecycle and ports."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/rehearsal-runner/src'))
spec = importlib.util.spec_from_file_location('scoped_lifecycle',
    ROOT / 'infra/envs/rehearsal-access/scoped_lifecycle.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from rehearsal_runner.isolation.profile import read_env
from rehearsal_runner.namespace import TEMPLATES
from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
from rehearsal_runner.recovery_lookup import recovery_identity
from rehearsal_runner.retry import AttemptPorts
from rehearsal_runner.runner import Namespace, T3Gate

NAME = 'rehearsal-' + 'a' * 24
PROFILE = read_env(TEMPLATES.parent / 'profiles/k3d.env')


class Kube:
    def __init__(self, fault=None):
        self.calls, self.grants, self.fault = [], {}, fault

    def run(self, *args, stdin=None):
        self.calls.append(args)
        if len(self.calls) == 1:
            body = {'metadata': {'name': NAME, 'uid': 'original-uid'}}
        elif stdin.startswith('{'):
            self.grants = {x['kind']: x for x in json.loads(stdin)['items']}
            if self.fault == 'create_unknown':
                raise TimeoutError()
            if self.fault == 'create_failed':
                return SimpleNamespace(returncode=1, stdout='')
            body = {}
        else:
            body = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body))

    def get_json(self, kind, *args):
        if kind != 'namespace':
            if self.fault == 'read_unknown':
                raise TimeoutError()
            obj = json.loads(json.dumps(self.grants[kind]))
            if self.fault == 'grant_changed' and kind == 'RoleBinding':
                obj['subjects'][0]['name'] = 'unexpected-runner'
            return obj
        labels, annotations = recovery_identity('run-1', 1)
        labels['app.kubernetes.io/part-of'] = 'bravo-rehearsal'
        uid = 'replacement-uid' if self.grants and self.fault == 'uid_changed' else 'original-uid'
        return {'metadata': {'uid': uid, 'labels': labels, 'annotations': annotations}}


class Cleaner:
    def __init__(self, confirmed=True):
        self.calls, self.confirmed = [], confirmed

    def cleanup(self, name, uid):
        self.calls.append((name, uid))
        return self.confirmed


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.admin, self.scoped, self.cleaner = Kube(), Kube(), Cleaner()
        self.ports = AttemptPorts(object(), NamespaceLifecycle(self.scoped, object(), PROFILE),
            T3Gate(self.scoped, [], 'test-image'), object())
        self.patch = patch('rehearsal_runner.namespace_lifecycle.secrets.token_hex', return_value='a'*24)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def compose(self):
        return module.scope_attempt(self.ports, provisioner=self.admin, cleaner=self.cleaner)

    def test_management_client_reaches_lifecycle_and_t3_only(self):
        result = self.compose()
        self.assertIs(result.proposer, self.ports.proposer)
        self.assertIs(result.verifier, self.ports.verifier)
        self.assertIs(result.isolation.kube, self.admin)
        self.assertIs(self.ports.lifecycle.kube, self.scoped)
        self.assertEqual(result.lifecycle.create('run-1'), Namespace(NAME, 'original-uid'))
        self.assertEqual(len(self.admin.calls), 3)
        self.assertEqual(self.scoped.calls, [])
        self.assertTrue(result.lifecycle.cleanup(Namespace(NAME, 'original-uid')))
        self.assertEqual(self.cleaner.calls, [(NAME, 'original-uid')])

    def test_scope_factory_is_lazy_and_preserves_existing_admission_and_attempt(self):
        seen = []
        inner_factory = SimpleNamespace(prepare=lambda a, f: (seen.append((a, f)) or self.ports))
        inner = SimpleNamespace(binding=lambda a: a, factory=lambda a, t: inner_factory)
        planner = module.ScopedPlanner(inner, self.admin, self.cleaner)
        self.assertEqual(planner.binding('actual-admission'), 'actual-admission')
        factory = planner.factory('actual-admission', 'registered-task')
        ports = factory.prepare(1, None)
        self.assertEqual(seen, [(1, None)])
        self.assertEqual(self.admin.calls, [])
        self.assertIs(ports.verifier, self.ports.verifier)

    def test_failed_or_unknown_grants_stop_and_clean_only_original_uid(self):
        for fault in ('create_failed', 'create_unknown', 'read_unknown', 'grant_changed', 'uid_changed'):
            with self.subTest(fault=fault):
                self.admin, self.cleaner = Kube(fault), Cleaner()
                with self.assertRaises((RuntimeError, TimeoutError)):
                    self.compose().lifecycle.create('run-1')
                self.assertEqual(self.cleaner.calls, [(NAME, 'original-uid')])
                self.assertEqual(len(self.admin.calls), 3)
                self.assertEqual(self.scoped.calls, [])

    def test_unknown_cleanup_is_not_success(self):
        self.cleaner.confirmed = False
        self.assertFalse(self.compose().lifecycle.cleanup(Namespace(NAME, 'original-uid')))

    def test_existing_wrapper_is_not_silently_discarded(self):
        with self.assertRaises(TypeError):
            module.scope_attempt(self.compose(), provisioner=self.admin, cleaner=self.cleaner)

    def test_grants_do_not_allow_cluster_mutation_exec_rbac_or_wildcards(self):
        values = module.attempt_grants(Namespace(NAME, 'original-uid'))['items']
        role, binding, cluster, global_binding = values
        for rule in role['rules']:
            self.assertNotIn('*', rule['resources'] + rule['verbs'])
            self.assertFalse(set(rule['verbs']) & {'bind', 'escalate', 'patch', 'update', 'delete'})
            self.assertFalse(set(rule['resources']) & {'namespaces', 'roles', 'rolebindings', 'pods/exec'})
        self.assertEqual(cluster['rules'], [{'apiGroups':[''], 'resources':['namespaces'],
            'resourceNames':[NAME], 'verbs':['get']}])
        for value in values:
            self.assertEqual(value['metadata']['ownerReferences'][0]['uid'], 'original-uid')
        self.assertEqual(binding['subjects'], global_binding['subjects'])
        self.assertEqual(binding['subjects'][0]['name'], 't15-runner')

    def test_arbitrary_namespace_is_rejected_before_creating_grants(self):
        for name in ('target-dev', 'rehearsal-existing', 'rehearsal-' + 'a'*23):
            with self.assertRaises(ValueError):
                module.attempt_grants(Namespace(name, 'uid'))


if __name__ == '__main__':
    unittest.main()
