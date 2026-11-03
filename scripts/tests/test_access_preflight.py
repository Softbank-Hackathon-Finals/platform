import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("access_preflight", Path(__file__).parents[1] / "access_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

ACCOUNT = "111122223333"
ROLE = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_Operator_example"
SESSION = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Operator_example/person@example.com"


class AccessPreflightTests(unittest.TestCase):
    def fixture(self, service, operation, region, **params):
        return {
            "get-caller-identity": {"Account": ACCOUNT, "Arn": SESSION},
            "list-instances": {"Instances": [{"InstanceArn": "private-instance-arn"}]},
            "list-permission-sets": {"PermissionSets": ["private-permission-set"]},
            "list-account-assignments": {"AccountAssignments": [{"PrincipalId": "private-user-id"}]},
            "list-clusters": {"clusters": ["private-cluster"]},
            "describe-cluster": {"cluster": {"status": "ACTIVE", "accessConfig": {"authenticationMode": "API"}, "endpoint": "https://private.example.com"}},
            "list-access-entries": {"accessEntries": [ROLE]},
        }[operation]

    def inspect(self, aws=None, **kw):
        return preflight.inspect(aws or Mock(read=Mock(side_effect=self.fixture)), ACCOUNT,
                                 "ap-northeast-2", "ap-northeast-2", **kw)

    def test_matching_inventory_does_not_claim_team_or_rbac_access(self):
        result = self.inspect(cluster="private-cluster", role=ROLE)
        self.assertTrue(all(c["state"] == "observed" for c in result["checks"]))
        self.assertEqual(result["team_access"], "NOT_VERIFIED")
        raw = json.dumps(result)
        for secret in (ACCOUNT, ROLE, SESSION, "private-user-id", "private-cluster", "private-instance-arn", "private.example.com"):
            self.assertNotIn(secret, raw)
        self.assertIn("each_user_sso_login", result["remaining_operational_checks"])

    def test_wrong_account_stops_before_service_inventory(self):
        aws = Mock(read=Mock(return_value={"Account": "222222222222", "Arn": SESSION}))
        result = self.inspect(aws)
        self.assertEqual(result["checks"][0]["state"], "mismatch")
        self.assertEqual(aws.read.call_count, 1)

    def test_access_denied_is_unknown_not_empty_or_success(self):
        def denied(service, operation, region, **params):
            if service == "sso-admin":
                raise preflight.ReadError("AccessDeniedException")
            return self.fixture(service, operation, region, **params)
        result = self.inspect(Mock(read=Mock(side_effect=denied)))
        row = next(c for c in result["checks"] if c["check"] == "identity_center")
        self.assertEqual(row, {"check": "identity_center", "state": "unknown", "error_code": "AccessDeniedException"})

    def test_iam_user_and_region_empty_response_remain_incomplete(self):
        def empty(service, operation, region, **params):
            if operation == "get-caller-identity":
                return {"Account": ACCOUNT, "Arn": "arn:aws:iam::111122223333:user/local"}
            if operation == "list-instances":
                return {"Instances": []}
            if operation == "list-clusters":
                return {"clusters": []}
            raise AssertionError(operation)
        result = self.inspect(Mock(read=Mock(side_effect=empty)))
        checks = {c["check"]: c for c in result["checks"]}
        self.assertEqual(checks["operator_sso_session"]["state"], "not_verified")
        self.assertEqual(checks["identity_center"]["state"], "missing")
        self.assertEqual(result["scope"]["identity_center_region"], "ap-northeast-2")

    def test_sts_session_cannot_be_used_as_eks_principal(self):
        aws = Mock()
        with self.assertRaisesRegex(ValueError, "iam_role"):
            self.inspect(aws, cluster="example", role=SESSION)
        aws.read.assert_not_called()

    def test_role_in_another_account_stops_before_any_aws_call(self):
        aws = Mock()
        with self.assertRaisesRegex(ValueError, "iam_role"):
            self.inspect(aws, role=ROLE.replace(ACCOUNT, "222222222222"))
        aws.read.assert_not_called()

    def test_unknown_authentication_mode_never_looks_ready(self):
        def legacy(service, operation, region, **params):
            if operation == "describe-cluster":
                return {"cluster": {"status": "ACTIVE", "accessConfig": {"authenticationMode": "CONFIG_MAP"}}}
            return self.fixture(service, operation, region, **params)
        aws = Mock(read=Mock(side_effect=legacy))
        result = self.inspect(aws, cluster="example", role=ROLE)
        self.assertEqual(next(c for c in result["checks"] if c["check"] == "source_cluster")["state"], "not_ready")
        self.assertFalse(any(c.args[1] == "list-access-entries" for c in aws.read.call_args_list))

    def test_cli_boundary_rejects_mutations_and_redacts_service_errors(self):
        client = preflight.AWS()
        with patch.object(preflight.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not_allowed"):
                client.read("eks", "create-access-entry", "ap-northeast-2")
            run.assert_not_called()
        error = subprocess.CompletedProcess([], 1, "", f"An error occurred (AccessDeniedException): {ROLE} with secret-value")
        with patch.object(preflight.subprocess, "run", return_value=error), self.assertRaisesRegex(preflight.ReadError, "^AccessDeniedException$"):
            client.read("eks", "list-clusters", "ap-northeast-2")

    def test_malformed_success_response_is_not_an_empty_inventory(self):
        def broken(service, operation, region, **params):
            if operation == "list-instances":
                return {}
            return self.fixture(service, operation, region, **params)
        result = self.inspect(Mock(read=Mock(side_effect=broken)))
        self.assertEqual(next(c for c in result["checks"] if c["check"] == "identity_center")["state"], "unknown")

    def test_incomplete_instance_cannot_skip_permission_set_check(self):
        def broken(service, operation, region, **params):
            if operation == "list-instances":
                return {"Instances": [{}]}
            return self.fixture(service, operation, region, **params)
        result = self.inspect(Mock(read=Mock(side_effect=broken)), cluster="private-cluster", role=ROLE)
        self.assertEqual(next(c for c in result["checks"] if c["check"] == "permission_sets")["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
