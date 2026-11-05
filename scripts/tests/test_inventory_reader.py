"""Reader isolation and preflight failures must precede any inventory execution."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("reader", Path(__file__).parents[1] / "inventory_reader.py")
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def scope():
    config = {"namespace": "source", "deployment": "app", "deployment_uid": "deployment-uid",
              "service_account": "app", "container": "app", "image": "registry/app@sha256:abc",
              "source_role_arns": ["example-app-role", "unrelated-role"], "database": "db", "database_uid": "db-uid"}
    deployment = {"metadata": {"namespace": "source", "name": "app", "uid": "deployment-uid"},
                  "spec": {"template": {"spec": {"serviceAccountName": "app", "containers": [
                      {"name": "app", "image": "registry/app@sha256:abc"}]}}}}
    sas = [{"metadata": {"namespace": "source", "name": name,
                         "annotations": {"eks.amazonaws.com/role-arn": role}}}
           for name, role in [("app", "example-app-role"), ("unused-secret-reader", "unrelated-role")]]
    exports = {"deployments.json": {"items": [deployment]}, "serviceaccounts.json": {"items": sas},
               "clusters.json": {"items": [{"metadata": {"namespace": "source", "name": "db", "uid": "db-uid"}}]}}
    return config, exports


class ReaderTests(unittest.TestCase):
    def test_all_namespace_irsa_roles_must_match_merged_371(self):
        config, exports = scope()
        reader.check_scope(exports, config)
        config["source_role_arns"] = ["example-app-role"]
        with self.assertRaisesRegex(RuntimeError, "source_iam_scope_changed"):
            reader.check_scope(exports, config)

    def test_literal_secret_is_refused_but_exact_secret_reference_is_allowed(self):
        config, exports = scope()
        container = exports["deployments.json"]["items"][0]["spec"]["template"]["spec"]["containers"][0]
        container["env"] = [{"name": "DB_SECRET_ID", "value": "example/db"}]
        with self.assertRaisesRegex(RuntimeError, "literal_credential_export_refused"):
            reader.check_scope(exports, config)
        config["allowed_credential_references"] = ["example/db"]
        reader.check_scope(exports, config)
        container["env"] = [{"name": "DB_PASSWORD", "value": "example/db"}]
        with self.assertRaisesRegex(RuntimeError, "literal_credential_export_refused"):
            reader.check_scope(exports, config)

    def test_stale_registration_blocks_scan_before_authentication(self):
        config = {"access_until": "2100-01-01T00:00:00+00:00", "source_sha": "new", "registered_source_sha": "old"}
        with tempfile.TemporaryDirectory() as td, patch.object(reader, "aws") as aws:
            with self.assertRaisesRegex(RuntimeError, "registration_binding_changed"):
                reader.collect(config, "personal", 18000, Path(td)/"run", "scan")
            aws.assert_not_called()

    def test_changed_deployment_identity_image_or_database_is_refused(self):
        config, exports = scope()
        for key in ["deployment_uid", "image", "database_uid", "source_role_arns"]:
            changed = copy.deepcopy(config)
            changed[key] = ["other-role"] if key == "source_role_arns" else "changed"
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                reader.check_scope(exports, changed)

    def test_cross_namespace_export_is_refused(self):
        config, exports = scope()
        exports["clusters.json"]["items"][0]["metadata"]["namespace"] = "outside"
        with self.assertRaisesRegex(RuntimeError, "namespace"):
            reader.check_scope(exports, config)

    def test_reader_has_no_inherited_admin_or_endpoint_configuration(self):
        contaminated = {"AWS_PROFILE": "admin", "AWS_ROLE_ARN": "admin",
                        "AWS_WEB_IDENTITY_TOKEN_FILE": "/tmp/admin", "AWS_ENDPOINT_URL": "https://wrong.invalid",
                        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://wrong.invalid", "PYTHONPATH": "/tmp/override"}
        creds = {"AccessKeyId": "synthetic-key", "SecretAccessKey": "synthetic-secret", "SessionToken": "synthetic-session"}
        with patch.dict(reader.os.environ, contaminated):
            env = reader.reader_environment(creds)
        for key in contaminated:
            self.assertNotIn(key, env)
        self.assertEqual(env["AWS_CONFIG_FILE"], "/dev/null")
        self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], "/dev/null")
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "synthetic-key")

    def test_expired_window_or_existing_output_never_authenticates(self):
        with tempfile.TemporaryDirectory() as td, patch.object(reader, "aws") as aws:
            with self.assertRaisesRegex(RuntimeError, "expired"):
                reader.collect({"access_until": "2000-01-01T00:00:00+00:00"}, "personal", 18000, Path(td)/"new", "verify")
            with self.assertRaisesRegex(RuntimeError, "output_exists"):
                reader.collect({"access_until": "2100-01-01T00:00:00+00:00"}, "personal", 18000, Path(td), "verify")
            aws.assert_not_called()

    def test_wrong_personal_identity_stops_before_role_assumption(self):
        config = {"access_until": "2100-01-01T00:00:00+00:00", "source_sha": "sha",
                  "source_environment": "source", "account": "synthetic", "allowed_sso_user_ids": ["expected"]}
        with tempfile.TemporaryDirectory() as td, patch.object(reader, "aws", return_value={"UserId": "other"}) as aws:
            out = Path(td)/"run"
            with self.assertRaisesRegex(RuntimeError, "identity_mismatch"):
                reader.collect(config, "personal", 18000, out, "verify")
            self.assertEqual(aws.call_count, 1)
            receipt = json.loads((out/"result.json").read_text())
            self.assertFalse(receipt["scan_started"])
            self.assertEqual(receipt["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
