import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "infra/envs/rehearsal-access/control_guard.py"
SPEC = importlib.util.spec_from_file_location("control_guard", PATH)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class ControlGuardTests(unittest.TestCase):
    def test_workloads_and_controllers_cannot_be_operator_exceptions(self):
        cases = [[], None, [None], [{}], [" alice"], ["a\nb"], ["alice", "alice"],
                 ["system:masters"],
                 ["system:serviceaccount:cnpg-system:cnpg-cloudnative-pg"],
                 [guard.JOB_CONTROLLER], [guard.GARBAGE_COLLECTOR], [guard.RUNNER]]
        for users in cases:
            with self.subTest(users=users), self.assertRaises(ValueError):
                guard.render(users)

    def test_explicit_personal_exceptions_do_not_grant_rbac(self):
        result = guard.render(["system:admin", "operator@example.invalid",
                               "system:serviceaccount:bravo-team-access:member-01"])
        self.assertEqual({x["kind"] for x in result["items"]}, {
            "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"})
        self.assertEqual(result["items"][1]["spec"]["validationActions"], ["Deny"])

    def test_service_account_token_secrets_are_denied_for_every_creator(self):
        items = guard.render(["operator@example.invalid"])["items"]
        policy = next(x for x in items if x["metadata"]["name"] == guard.TOKEN_SECRETS
                      and x["kind"] == "ValidatingAdmissionPolicy")
        binding = next(x for x in items if x["metadata"]["name"] == guard.TOKEN_SECRETS
                       and x["kind"] == "ValidatingAdmissionPolicyBinding")
        rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
        self.assertEqual((rule["resources"], rule["operations"]), (["secrets"], ["CREATE", "UPDATE"]))
        self.assertEqual(policy["spec"]["matchConstraints"]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": guard.NAMESPACE})
        expression = policy["spec"]["validations"][0]["expression"]
        self.assertIn("kubernetes.io/service-account-token", expression)
        self.assertNotIn("userInfo", expression)  # no operator or runner exception
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])


if __name__ == "__main__":
    unittest.main()
