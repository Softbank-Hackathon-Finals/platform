import configparser
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("operator_sso", Path(__file__).parents[1] / "verify_operator_sso.py")
sso = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sso)

CONFIG = """[profile operator]
sso_session = bravo
sso_account_id = 111122223333
sso_role_name = BravoOperator
[sso-session bravo]
sso_start_url = https://example.awsapps.com/start
sso_region = ap-northeast-2
"""


class OperatorSSOTests(unittest.TestCase):
    def test_no_default_fallback(self):
        config = configparser.RawConfigParser()
        config.read_string("[default]\nregion=ap-northeast-2\n")
        self.assertEqual(sso.inspect_profile(config, "operator"), "SSO_PROFILE_MISSING")

    def test_mixed_credential_process_rejected(self):
        config = configparser.RawConfigParser()
        config.read_string(CONFIG.replace("sso_session = bravo", "sso_session = bravo\ncredential_process = other"))
        self.assertEqual(sso.inspect_profile(config, "operator"), "MIXED_CREDENTIAL_SOURCES")

    def test_iam_user_is_not_sso(self):
        self.assertEqual(sso.check_identity({"Account": "111122223333", "Arn": "arn:aws:iam::111122223333:user/operator"}, "111122223333", "BravoOperator"), "NOT_EXPECTED_SSO_ROLE")

    def test_other_account_rejected(self):
        self.assertEqual(sso.check_identity({"Account": "999900001111"}, "111122223333", "BravoOperator"), "ACCOUNT_MISMATCH")

    def test_exact_permission_set_role(self):
        identity = {"Account": "111122223333", "Arn": "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoOperator_0123456789abcdef/session"}
        self.assertEqual(sso.check_identity(identity, "111122223333", "BravoOperator"), "SSO_SESSION_OBSERVED")
        self.assertEqual(sso.check_identity(identity, "111122223333", "OtherOperator"), "NOT_EXPECTED_SSO_ROLE")

    def test_profile_mismatch_stops_before_aws(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(CONFIG)
            with patch.object(sso.subprocess, "run") as call:
                result = sso.run("operator", "999900001111", path)
                self.assertFalse(result["sso_verified"])
                call.assert_not_called()

    def test_ambient_keys_excluded_and_expiry_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(CONFIG)
            with patch.dict(sso.os.environ, {"AWS_ACCESS_KEY_ID": "ambient", "AWS_SESSION_TOKEN": "ambient"}):
                with patch.object(sso.subprocess, "run") as call:
                    call.return_value.returncode = 1
                    result = sso.run("operator", "111122223333", path)
                    self.assertEqual(result["status"], "SSO_CALL_FAILED")
                    self.assertNotIn("AWS_ACCESS_KEY_ID", call.call_args.kwargs["env"])
                    self.assertNotIn("AWS_SESSION_TOKEN", call.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
