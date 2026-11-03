import configparser
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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
        self.assertEqual(sso.check_identity({"Account": "222222222222"}, "111122223333", "BravoOperator"), "ACCOUNT_MISMATCH")

    def test_exact_permission_set_role(self):
        identity = {"Account": "111122223333", "Arn": "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoOperator_0123456789abcdef/session"}
        self.assertEqual(sso.check_identity(identity, "111122223333", "BravoOperator"), "SSO_SESSION_OBSERVED")
        self.assertEqual(sso.check_identity(identity, "111122223333", "OtherOperator"), "NOT_EXPECTED_SSO_ROLE")

    def test_profile_mismatch_stops_before_aws(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(CONFIG)
            with patch.object(sso.subprocess, "run") as call:
                result = sso.run("operator", "222222222222", path)
                self.assertFalse(result["sso_verified"])
                call.assert_not_called()

    def test_ambient_keys_excluded_and_expiry_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(CONFIG)
            with patch.dict(sso.os.environ, {"AWS_ACCESS_KEY_ID": "ambient", "AWS_SESSION_TOKEN": "ambient"}), \
                    patch.object(sso.subprocess, "run") as call:
                call.return_value.returncode = 1
                result = sso.run("operator", "111122223333", path)
                self.assertEqual(result["status"], "SSO_CALL_FAILED")
                self.assertNotIn("AWS_ACCESS_KEY_ID", call.call_args.kwargs["env"])
                self.assertNotIn("AWS_SESSION_TOKEN", call.call_args.kwargs["env"])

    def test_invalid_provider_shape_cannot_pass_or_raise_raw_errors(self):
        for identity in (None, [], "private context", {}, {"Account": 111122223333},
                         {"Account": "111122223333", "Arn": ["private context"]}):
            with self.subTest(identity=identity):
                self.assertEqual(sso.check_identity(identity, "111122223333", "BravoOperator"), "SSO_RESPONSE_INVALID")

    def test_unreadable_config_stops_before_provider_call(self):
        with patch.object(Path, "open", side_effect=PermissionError("sensitive input path")), \
                patch.object(sso.subprocess, "run") as provider:
            result = sso.run("operator", "111122223333", Path("private-config"))
        self.assertEqual(result, {"status": "SSO_CONFIG_UNAVAILABLE", "sso_verified": False})
        provider.assert_not_called()

    def test_cli_config_failures_have_only_safe_json_and_nonzero_exit(self):
        marker = "private-input-canary"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / marker
            for content, status in [
                (marker.encode(), "SSO_CONFIG_INVALID"),
                (b"[profile operator]\nname=\xff", "SSO_CONFIG_INVALID"),
                (None, "SSO_CONFIG_UNAVAILABLE"),
            ]:
                with self.subTest(status=status, content=content):
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    env = dict(os.environ, AWS_CONFIG_FILE=str(path), BRAVO_EXPECTED_AWS_ACCOUNT="111122223333", PATH="")
                    result = subprocess.run([sys.executable, str(Path(sso.__file__)), "--profile", "operator"],
                                            env=env, capture_output=True, text=True, check=False, timeout=10)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(json.loads(result.stdout), {"status": status, "sso_verified": False})
                    self.assertEqual(result.stderr, "")
                    self.assertNotIn(marker, result.stdout)
                    self.assertNotIn(directory, result.stdout)

    def test_provider_json_shape_failure_remains_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(CONFIG)
            with patch.object(sso.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]")):
                result = sso.run("operator", "111122223333", path)
            self.assertEqual(result["status"], "SSO_RESPONSE_INVALID")
            self.assertFalse(result["sso_verified"])


if __name__ == "__main__":
    unittest.main()
