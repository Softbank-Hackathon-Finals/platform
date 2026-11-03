"""Copy entry point without object-store or database dependencies; runs in default discovery."""
import ast
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(os.environ.get("T8_ATTACH_MODULE_DIR")
                 or Path(__file__).resolve().parents[2] / "infra/envs/target-workloads")
sys.path.append(str(WORKLOADS))
attachment_copy = importlib.import_module("attachment_copy")

SECRET_NAME = "attachments/private-name"


class Store:
    def __init__(self, values, sizes=None):
        self.values = values.copy()
        self.reported_sizes = sizes
        self.writes = []

    def list_keys(self):
        return list(self.values)

    def sizes(self):
        return self.reported_sizes or {key: len(value) for key, value in self.values.items()}

    def read(self, key):
        return self.values[key]

    def create(self, key, value):
        self.values[key] = value
        self.writes.append(key)


class Connection:
    def __init__(self, keys):
        self.keys = keys
        self.queries = []

    def execute(self, query, parameters):
        self.queries.append((query, parameters))
        return self

    def fetchall(self):
        return [(key,) for key in self.keys]


class ReferencedKeyTests(unittest.TestCase):
    def test_reads_only_object_keys_one_past_the_limit(self):
        connection = Connection(["a", "b"])
        self.assertEqual(attachment_copy.read_referenced_keys(connection), ["a", "b"])
        (query, parameters), = connection.queries
        self.assertTrue(query.startswith("SELECT DISTINCT object_key FROM public.attachments"))
        self.assertEqual(parameters, (attachment_copy.MAX_OBJECTS + 1,))


class CopyKeysTests(unittest.TestCase):
    def test_receipt_holds_only_counts_and_hashes(self):
        receipt = attachment_copy.copy_keys(Store({SECRET_NAME: b"body"}), Store({}), [SECRET_NAME])
        self.assertEqual(set(receipt), {
            "passed", "source_count", "destination_count", "database_referenced_count",
            "operator_approved_extra_count", "copied", "reused", "bytes_verified",
            "key_content_inventory_sha256", "content_sha256"})
        self.assertNotIn(SECRET_NAME, json.dumps(receipt))
        self.assertNotIn("body", json.dumps(receipt).replace("passed", ""))

    def test_total_size_is_rejected_before_any_destination_write(self):
        half_limit = attachment_copy.MAX_BYTES // 2 + 1
        source = Store({"a": b"1", "b": b"2"}, sizes={"a": half_limit, "b": half_limit})
        destination = Store({})
        with self.assertRaisesRegex(ValueError, "bounded transfer size exceeded"):
            attachment_copy.copy_keys(source, destination, ["a", "b"])
        self.assertEqual(destination.writes, [])

    def test_total_size_exactly_at_the_limit_is_allowed(self):
        half_limit = attachment_copy.MAX_BYTES // 2
        source = Store({"a": b"1", "b": b"2"}, sizes={"a": half_limit, "b": half_limit})
        destination = Store({})
        attachment_copy.copy_keys(source, destination, ["a", "b"])
        self.assertEqual(destination.writes, ["a", "b"])


class VerifiedTlsTests(unittest.TestCase):
    def test_remote_database_requires_verify_full(self):
        attachment_copy.require_verified_tls("source-db.internal", "verify-full")
        for mode in (None, "disable", "prefer", "require", "verify-ca"):
            with self.assertRaises(attachment_copy.AttachmentCopyFailed):
                attachment_copy.require_verified_tls("source-db.internal", mode)

    def test_loopback_test_database_may_skip_tls(self):
        attachment_copy.require_verified_tls("127.0.0.1", "disable")


def environment(**overrides):
    values = {
        "ATTACHMENT_SOURCE_BUCKET": "source-bucket", "ATTACHMENT_SOURCE_REGION": "ap-northeast-1",
        "ATTACHMENT_SOURCE_BUCKET_OWNER": "123456789012",
        "ATTACHMENT_TARGET_ENDPOINT_URL": "http://target.invalid:8333", "ATTACHMENT_TARGET_BUCKET": "target-bucket",
        "ATTACHMENT_TARGET_ACCESS_KEY_ID": "private-key-id", "ATTACHMENT_TARGET_SECRET_ACCESS_KEY": "private-secret",
    }
    values.update(overrides)
    return {name: value for name, value in values.items() if value is not None}


class ConfigurationTests(unittest.TestCase):
    def failure(self, **overrides):
        with self.assertRaises(attachment_copy.AttachmentCopyFailed) as caught:
            attachment_copy.run_copy(environment(**overrides))
        self.assertEqual(caught.exception.stage, "configuration")
        return caught.exception

    def test_missing_value_names_the_variable_only(self):
        for name in ("ATTACHMENT_SOURCE_BUCKET", "ATTACHMENT_SOURCE_REGION", "ATTACHMENT_TARGET_ENDPOINT_URL",
                     "ATTACHMENT_TARGET_BUCKET", "ATTACHMENT_TARGET_ACCESS_KEY_ID",
                     "ATTACHMENT_TARGET_SECRET_ACCESS_KEY"):
            failure = self.failure(**{name: None})
            self.assertEqual(failure.reason, "missing " + name)
            self.assertNotIn("private", str(failure.receipt()))

    def test_real_s3_requires_the_expected_bucket_owner(self):
        self.assertEqual(self.failure(ATTACHMENT_SOURCE_BUCKET_OWNER=None).reason,
                         "missing ATTACHMENT_SOURCE_BUCKET_OWNER")

    def test_same_store_on_both_sides_cannot_report_a_false_copy(self):
        failure = self.failure(ATTACHMENT_SOURCE_ENDPOINT_URL="http://target.invalid:8333",
                               ATTACHMENT_SOURCE_BUCKET="target-bucket")
        self.assertEqual(failure.reason, "source and destination are the same bucket")

    def test_unreadable_extra_inventory_is_a_configuration_failure(self):
        failure = self.failure(ATTACHMENT_APPROVED_EXTRA_KEYS_FILE="/nonexistent/private-path")
        self.assertEqual((failure.reason, failure.error_category), ("unclassified", "FileNotFoundError"))
        self.assertNotIn("private-path", str(failure.receipt()))

    def test_extra_inventory_file_ignores_blank_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".keys") as stream:
            stream.write("first\n\n  \nsecond\n")
            stream.flush()
            extras = attachment_copy.approved_extras_from({"ATTACHMENT_APPROVED_EXTRA_KEYS_FILE": stream.name})
        self.assertEqual(extras, ["first", "second"])


class SafeFailureTests(unittest.TestCase):
    def describe(self, error):
        return attachment_copy.describe_failure("transfer", error)

    def test_unknown_message_is_never_published(self):
        failure = self.describe(ValueError(SECRET_NAME))
        self.assertEqual((failure.stage, failure.reason), ("transfer", "unclassified"))
        self.assertNotIn(SECRET_NAME, str(failure))
        self.assertNotIn(SECRET_NAME, json.dumps(failure.receipt()))

    def test_known_synchronize_reason_is_published(self):
        reason = "source bucket and database attachment inventory differ"
        self.assertEqual(self.describe(ValueError(reason)).reason, reason)

    def test_only_listed_s3_codes_are_published(self):
        class S3Error(Exception):
            def __init__(self, code):
                super().__init__(SECRET_NAME)
                self.response = {"Error": {"Code": code, "Message": SECRET_NAME}}

        self.assertEqual(self.describe(S3Error("AccessDenied")).s3_error_code, "AccessDenied")
        leaked = self.describe(S3Error(SECRET_NAME))
        self.assertEqual(leaked.s3_error_code, "unclassified")
        self.assertNotIn(SECRET_NAME, json.dumps(leaked.receipt()))

    def test_logged_traceback_carries_no_raw_message(self):
        # The Executor logs what it catches, so the chained original must not print with it.
        def raises_private_error(_):
            raise ValueError(SECRET_NAME)

        with patch.object(attachment_copy, "approved_extras_from", raises_private_error), \
                self.assertRaises(attachment_copy.AttachmentCopyFailed) as caught:
            attachment_copy.run_copy({})
        failure = caught.exception
        logged = "".join(traceback.format_exception(type(failure), failure, failure.__traceback__))
        self.assertIn("attachment copy failed at configuration", logged)
        self.assertNotIn(SECRET_NAME, logged)

    def test_every_synchronize_message_is_a_public_reason(self):
        tree = ast.parse((WORKLOADS / "sync_attachments.py").read_text())
        messages = {node.exc.args[0].value for node in ast.walk(tree)
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args
                    and isinstance(node.exc.args[0], ast.Constant)}
        self.assertTrue(messages)
        self.assertEqual(messages - attachment_copy.PUBLIC_REASONS, set())


class CommandTests(unittest.TestCase):
    def run_main(self, outcome):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(attachment_copy, "run_copy", outcome), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = attachment_copy.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_prints_one_json_receipt_and_exits_zero(self):
        code, stdout, stderr = self.run_main(lambda: {"passed": True, "copied": 1})
        self.assertEqual((code, json.loads(stdout), stderr), (0, {"passed": True, "copied": 1}, ""))
        self.assertEqual(len(stdout.splitlines()), 1)

    def test_failure_prints_safe_json_and_exits_nonzero(self):
        def fail():
            raise attachment_copy.describe_failure("transfer", ValueError(SECRET_NAME))

        code, stdout, _ = self.run_main(fail)
        self.assertEqual(code, 1)
        self.assertIs(json.loads(stdout)["passed"], False)
        self.assertNotIn(SECRET_NAME, stdout)


if __name__ == "__main__":
    unittest.main()
