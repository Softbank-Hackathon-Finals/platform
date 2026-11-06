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


if __name__ == "__main__":
    unittest.main()
