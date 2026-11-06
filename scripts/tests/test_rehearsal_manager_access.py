"""T15 manager account: separate from the runner, confined by a deny policy installed first."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('manager_access',
    ROOT / 'infra/envs/rehearsal-access/manager_access.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lifecycle_spec = importlib.util.spec_from_file_location('scoped_lifecycle_for_manager',
    ROOT / 'infra/envs/rehearsal-access/scoped_lifecycle.py')
lifecycle = importlib.util.module_from_spec(lifecycle_spec)
lifecycle_spec.loader.exec_module(lifecycle)
TEMPLATES = ROOT / 'services/rehearsal-runner/templates'


class ManagerAccess(unittest.TestCase):
    def setUp(self):
        self.items = module.render()['items']
        self.kinds = [o['kind'] for o in self.items]

    def test_policy_precedes_any_permission_and_matches_only_the_manager(self):
        self.assertEqual(self.kinds, ['ValidatingAdmissionPolicy', 'ValidatingAdmissionPolicyBinding'] * 3
                         + ['ServiceAccount', 'ClusterRole', 'ClusterRoleBinding'])
        for policy, binding in zip(self.items[0:6:2], self.items[1:6:2]):
            self.assertEqual(policy['spec']['failurePolicy'], 'Fail')
            self.assertEqual(binding['spec'], {'policyName': policy['metadata']['name'],
                                               'validationActions': ['Deny']})
            self.assertEqual(policy['spec']['matchConditions'][0]['expression'],
                'request.userInfo.username == "system:serviceaccount:bravo-rehearsal-control:t15-manager"')
        self.assertNotEqual(module.MANAGER, lifecycle.RUNNER)
        self.assertEqual(module.RUNNER, lifecycle.RUNNER)
        self.assertEqual(module.NAMESPACE, lifecycle.RUNNER_NAMESPACE)

    def test_every_granted_resource_is_policed(self):
        policed = {(g, r) for policy in self.items[0:6:2]
                   for rule in policy['spec']['matchConstraints']['resourceRules']
                   for g in rule['apiGroups'] for r in rule['resources']}
        for rule in self.items[7]['rules']:
            for group in rule['apiGroups']:
                for resource in rule['resources']:
                    self.assertIn((group, resource), policed)
            self.assertNotIn('*', rule['verbs'] + rule['resources'] + rule['apiGroups'])
            self.assertFalse({'list', 'watch', 'update', 'impersonate'} & set(rule['verbs']))
        self.assertNotIn('secrets', json.dumps(self.items[7]['rules']))
        self.assertFalse(self.items[6]['automountServiceAccountToken'])

    def test_rbac_covers_the_objects_t3_and_attempt_grants_write(self):
        granted = {r for rule in self.items[7]['rules'] for r in rule['resources']}
        plural = {'Namespace': 'namespaces', 'ServiceAccount': 'serviceaccounts', 'Role': 'roles',
                  'RoleBinding': 'rolebindings', 'NetworkPolicy': 'networkpolicies',
                  'ResourceQuota': 'resourcequotas', 'LimitRange': 'limitranges',
                  'ClusterRole': 'clusterroles', 'ClusterRoleBinding': 'clusterrolebindings'}
        kinds = {line.split(':', 1)[1].strip() for path in TEMPLATES.glob('*.yaml')
                 for line in path.read_text().splitlines() if line.startswith('kind:')}
        namespace = type('N', (), {'name': 'rehearsal-' + 'a' * 24, 'uid': 'u'})()
        kinds |= {o['kind'] for o in lifecycle.attempt_grants(namespace)['items']}
        self.assertEqual({plural[k] for k in kinds}, granted)


if __name__ == '__main__':
    unittest.main()
