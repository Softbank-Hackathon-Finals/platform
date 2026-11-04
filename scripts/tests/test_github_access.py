"""Access proof needs positive controls and verified probe cleanup."""

import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
access = importlib.import_module("verify_github_access")
BASE = "a" * 40
OTHER = "b" * 40
REPOSITORY = "example/demo"
PROBE = "c" * 32


class Server:
    def __init__(self):
        self.refs = {}
        self.calls = []
        self.reader_status = 403
        self.remaining = "100"
        self.denial_message = "Resource not accessible by personal access token"
        self.fail_after = False
        self.create_timeout = False
        self.advance_ref = False
        self.delete_status = 204

    def client(self, role):
        server = self

        class FakeClient:
            def request(self, method, path, payload=None):
                server.calls.append((role, method, path, payload))
                if path == "":
                    return 200, {"id": 1, "full_name": REPOSITORY, "default_branch": "main"}, {}
                if path == "/git/ref/heads/main":
                    return 200, {"object": {"sha": BASE}}, {}
                if path == f"/git/commits/{BASE}":
                    return 200, {"sha": BASE}, {}
                if method == "POST":
                    if role == "reader" and server.reader_status != 201:
                        return server.reader_status, {"message": server.denial_message}, {
                            "X-RateLimit-Remaining": server.remaining,
                            "X-Accepted-GitHub-Permissions": "contents=write",
                        }
                    if server.fail_after and payload["ref"].endswith("writer_after"):
                        return 503, {}, {}
                    server.refs[payload["ref"][5:]] = payload["sha"]
                    if server.create_timeout:
                        raise access.ObservationError("transport_unknown")
                    return 201, {"ref": payload["ref"], "object": {"sha": payload["sha"]}}, {}
                if method == "GET":
                    name = path.removeprefix("/git/ref/")
                    if name not in server.refs:
                        return 404, {}, {}
                    # Simulate an unrelated actor moving the probe before cleanup.
                    if server.advance_ref and role == "writer":
                        server.refs[name] = OTHER
                    return 200, {"object": {"sha": server.refs[name]}}, {}
                if method == "DELETE":
                    if server.delete_status == 204:
                        del server.refs[path.removeprefix("/git/refs/")]
                    return server.delete_status, {}, {}
                raise AssertionError("unexpected request")

        return FakeClient()

    def verify(self):
        return access.verify(REPOSITORY, 1, self.client("reader"), self.client("writer"), PROBE)


class GithubAccessTest(unittest.TestCase):
    def test_controls_denial_and_cleanup_are_all_required(self):
        server = Server()
        result = server.verify()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["cleanup_verified"])
        self.assertFalse(server.refs)
        self.assertFalse(result["t31_consumer_verified"])
        writes = [(role, path, body) for role, method, path, body in server.calls if method == "POST"]
        self.assertEqual([role for role, _, _ in writes], ["writer", "reader", "writer"])
        self.assertTrue(all(body["ref"].startswith("refs/heads/t7-access-probe/") for _, _, body in writes))

    def test_reader_with_write_permission_fails_and_is_cleaned(self):
        server = Server()
        server.reader_status = 201
        result = server.verify()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["reader_write_denied"])
        self.assertTrue(result["cleanup_verified"])
        self.assertFalse(server.refs)

    def test_rate_limit_generic_forbidden_auth_and_network_are_unknown(self):
        for status, remaining, message in [(403, "0", "Resource not accessible by personal access token"),
                                           (403, "100", "Forbidden"), (401, "100", "Bad credentials"),
                                           (404, "100", "Not Found"), (503, "100", "Unavailable")]:
            with self.subTest(status=status, remaining=remaining, message=message):
                server = Server()
                server.reader_status, server.remaining, server.denial_message = status, remaining, message
                result = server.verify()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertTrue(result["cleanup_verified"])
                self.assertFalse(server.refs)

    def test_failed_after_control_cannot_pass(self):
        server = Server()
        server.fail_after = True
        result = server.verify()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["checks"]["reader_write_denied"])
        self.assertFalse(server.refs)

    def test_lost_create_response_still_cleans_owned_branch(self):
        server = Server()
        server.create_timeout = True
        result = server.verify()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["cleanup_verified"])
        self.assertFalse(server.refs)

    def test_cleanup_failure_keeps_unknown_and_branch_identity(self):
        server = Server()
        server.delete_status = 503
        result = server.verify()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["cleanup_verified"])
        self.assertEqual(set(server.refs), {x for x in result["probe_refs"] if "writer" in x})

    def test_changed_branch_is_preserved(self):
        server = Server()
        server.advance_ref = True
        result = server.verify()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["cleanup_verified"])
        self.assertTrue(server.refs)
        self.assertFalse(any(method == "DELETE" for _, method, _, _ in server.calls))

    def test_wrong_repository_identity_does_not_write(self):
        server = Server()
        result = access.verify(REPOSITORY, 2, server.client("reader"), server.client("writer"), PROBE)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(any(method != "GET" for _, method, _, _ in server.calls))

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(access.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_body_read_failure_is_unknown_without_provider_error_text(self):
        secret = "never-print-this-provider-fixture"
        for error in (TimeoutError(secret), access.HTTPException(secret)):
            with self.subTest(error=type(error).__name__):
                client = access.Client(REPOSITORY, secret)
                response = MagicMock()
                response.read.side_effect = error
                with patch.object(client.opener, "open", return_value=response):
                    with self.assertRaisesRegex(access.ObservationError, "^transport_unknown$"):
                        client.request("POST", "/git/refs", {"ref": "refs/heads/probe", "sha": BASE})
                response.__exit__.assert_called_once()

    def test_cli_exit_codes_follow_observed_outcome(self):
        for status, exit_code in ((403, 0), (201, 1), (503, 2)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                server = Server()
                server.reader_status = status
                receipt = Path(temporary) / "receipt.json"
                argv = [access.__file__, "--repository", REPOSITORY, "--repository-id", "1",
                        "--receipt", str(receipt), "--write-probe"]
                tokens = {"reader": "github_pat_" + "r" * 30, "writer": "github_pat_" + "w" * 30}
                with patch.object(sys, "argv", argv), patch.object(sys, "stdin", io.StringIO(json.dumps(tokens))), \
                        patch.object(sys, "stdout", io.StringIO()) as stdout, \
                        patch.object(access, "Client", side_effect=[server.client("reader"), server.client("writer")]):
                    self.assertEqual(access.main(), exit_code)
                content = receipt.read_text() + stdout.getvalue()
                for token in tokens.values():
                    self.assertNotIn(token, content)

    def test_cli_rejects_ambient_credentials_and_keeps_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            command = [sys.executable, access.__file__, "--repository", REPOSITORY,
                       "--repository-id", "1", "--receipt", str(receipt), "--write-probe"]
            secret = "never-print-this-fixture-value"
            result = subprocess.run(command, check=False, input=json.dumps({"reader": secret, "writer": secret}),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(secret, result.stdout + result.stderr + receipt.read_text())
            original = receipt.read_bytes()
            repeat = subprocess.run(command, check=False, input="{}", capture_output=True, text=True)
            self.assertNotEqual(repeat.returncode, 0)
            self.assertEqual(receipt.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
