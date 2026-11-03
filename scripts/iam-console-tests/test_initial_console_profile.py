import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("console", Path(__file__).parents[1] / "create_initial_console_profile.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class InitialConsoleTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sts, self.iam = Mock(), Mock()
        self.session.client.side_effect = lambda service, **kw: self.sts if service == "sts" else self.iam
        self.request = {"expected_account": "111122223333", "user_name": "example-human", "password": "EXAMPLE_ONLY"}
        self.sts.get_caller_identity.return_value = {"Account": "111122223333"}
        self.iam.get_user.return_value = {"User": {"Path": "/team-bravo/humans/", "Tags": [
            {"Key": "principal-type", "Value": "human"}, {"Key": "project", "Value": "softbank-finals"}]}}

    def test_new_profile_requires_change_and_readback(self):
        self.iam.get_login_profile.side_effect = [ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetLoginProfile"),
                                                  {"LoginProfile": {"PasswordResetRequired": True}}]
        result = console.create(self.session, self.request)
        self.iam.create_login_profile.assert_called_once_with(UserName="example-human", Password="EXAMPLE_ONLY", PasswordResetRequired=True)
        self.assertEqual(result["console_profile"], "created")
        self.assertNotIn("EXAMPLE_ONLY", str(result))

    def test_existing_profile_is_preserved(self):
        result = console.create(self.session, self.request)
        self.assertEqual(result["console_profile"], "existing_preserved")
        self.iam.create_login_profile.assert_not_called()

    def test_wrong_account_and_nonhuman_reject_before_write(self):
        self.sts.get_caller_identity.return_value = {"Account": "999988887777"}
        with self.assertRaisesRegex(ValueError, "account_boundary"):
            console.create(self.session, self.request)
        self.iam.get_user.assert_not_called()
        self.sts.get_caller_identity.return_value = {"Account": "111122223333"}
        self.iam.get_user.return_value["User"]["Path"] = "/runtime/"
        with self.assertRaisesRegex(ValueError, "not_managed"):
            console.create(self.session, self.request)
        self.iam.create_login_profile.assert_not_called()

    def test_read_failure_is_not_missing_profile(self):
        self.iam.get_login_profile.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetLoginProfile")
        with self.assertRaises(ClientError):
            console.create(self.session, self.request)
        self.iam.create_login_profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
