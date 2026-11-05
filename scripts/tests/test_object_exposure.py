"""Demo exposure states for the target object store; no cluster or network in default discovery."""
import importlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
sys.path.append(str(WORKLOADS))
exposure = importlib.import_module("object_exposure")

ENV = {
    "BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY": "BOOTSTRAPFIXTURE0001",
    "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY": "bootstrap-fixture-secret-value-not-real-0001",
    "BRAVO_OBJECT_CLIENT_ACCESS_KEY": "CLIENTFIXTURE0000001",
    "BRAVO_OBJECT_CLIENT_SECRET_KEY": "client-fixture-secret-value-not-real-000001",
}
BUCKET = "demo-chat-attachments"


class RenderTests(unittest.TestCase):
    def test_closed_state_has_no_anonymous_identity_and_exact_bucket_scope(self):
        doc = exposure.render("closed", BUCKET, ENV)
        names = [i["name"] for i in doc["identities"]]
        self.assertEqual(names, ["bootstrap", "client"])
        for identity in doc["identities"]:
            for action in identity["actions"]:
                self.assertTrue(action.endswith(":" + BUCKET), action)
        self.assertNotIn("Admin:" + BUCKET, doc["identities"][1]["actions"])

    def test_open_demo_adds_read_only_anonymous_and_requires_acknowledgement(self):
        with self.assertRaises(ValueError):
            exposure.render("open-demo", BUCKET, ENV)
        with self.assertRaises(ValueError):
            exposure.render("open-demo", BUCKET, ENV, acknowledge_open=True, demo_run_id="")
        doc = exposure.render("open-demo", BUCKET, ENV, acknowledge_open=True, demo_run_id="demo-2026-11-07")
        anonymous = [i for i in doc["identities"] if i["name"] == "anonymous"]
        self.assertEqual(len(anonymous), 1)
        self.assertEqual(anonymous[0]["actions"], ["Read:" + BUCKET])
        self.assertNotIn("credentials", anonymous[0])
        closed = exposure.render("closed", BUCKET, ENV)
        self.assertEqual(doc["identities"][:2], closed["identities"])

    def test_bucket_and_credential_validation(self):
        for bad in ("*", "Demo-Chat", "a", "demo..chat", "demo/chat"):
            with self.assertRaises(ValueError):
                exposure.render("closed", bad, ENV)
        missing = dict(ENV, BRAVO_OBJECT_CLIENT_SECRET_KEY="short")
        with self.assertRaises(ValueError):
            exposure.render("closed", BUCKET, missing)
        same = dict(ENV, BRAVO_OBJECT_CLIENT_ACCESS_KEY=ENV["BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY"])
        with self.assertRaises(ValueError):
            exposure.render("closed", BUCKET, same)

    def test_cli_writes_private_file_and_never_prints_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV, clear=False):
            output = Path(directory) / "s3.json"
            out = io.StringIO()
            with redirect_stdout(out):
                code = exposure.main(["render", "--state", "open-demo", "--bucket", BUCKET, "--output", str(output),
                                      "--acknowledge-anonymous-read", "--demo-run-id", "demo-2026-11-07"])
            self.assertEqual(code, 0)
            printed = out.getvalue()
            for secret in (ENV["BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY"], ENV["BRAVO_OBJECT_CLIENT_SECRET_KEY"]):
                self.assertNotIn(secret, printed)
            summary = json.loads(printed)
            self.assertEqual(summary["anonymous_actions"], ["Read:" + BUCKET])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertIn(ENV["BRAVO_OBJECT_CLIENT_SECRET_KEY"], output.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                exposure.main(["render", "--state", "closed", "--output", str(output)])


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return b"x"


class ProbeTests(unittest.TestCase):
    def test_probe_classifies_open_closed_and_unknown(self):
        endpoint = "http://seaweedfs.target-dev.svc.cluster.local:8333"
        open_result = exposure.probe(endpoint, BUCKET, "t8/canary.txt", opener=lambda req, timeout: FakeResponse(200))

        def forbidden(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        def unreachable(req, timeout):
            raise urllib.error.URLError("refused")

        closed_result = exposure.probe(endpoint, BUCKET, "t8/canary.txt", opener=forbidden)
        unknown_result = exposure.probe(endpoint, BUCKET, "t8/canary.txt", opener=unreachable)
        self.assertEqual((open_result["anonymous_get"], open_result["state_observed"]), ("ALLOWED", "open"))
        self.assertEqual((closed_result["anonymous_get"], closed_result["state_observed"]), ("DENIED", "closed"))
        self.assertEqual(unknown_result["state_observed"], "unknown")
        self.assertTrue(unknown_result["anonymous_get"].startswith("UNKNOWN:"))
        self.assertNotIn("seaweedfs", json.dumps(open_result))

    def test_probe_rejects_unsafe_inputs(self):
        with self.assertRaises(ValueError):
            exposure.probe("http://host:8333/path", BUCKET, "k")
        with self.assertRaises(ValueError):
            exposure.probe("http://host:8333", BUCKET, "../etc")


class PlanTests(unittest.TestCase):
    def test_plan_lists_apply_restart_probe_and_gate_in_order(self):
        steps = exposure.plan("open-demo", BUCKET)["steps"]
        self.assertIn("render --state open-demo", steps[0])
        self.assertIn("kubectl apply", steps[1])
        self.assertIn("rollout restart", steps[2])
        self.assertIn("state_observed=open", steps[4])
        self.assertIn("IAM-01.1", steps[5])


if __name__ == "__main__":
    unittest.main()


class _Handler(BaseHTTPRequestHandler):
    """Loopback object store: distinguishes the exact encoded key, redirects one key to /login."""

    def log_message(self, *args):
        return

    def do_GET(self):
        routes = {
            "/demo-chat-attachments/open.txt": (200, b"public"),
            "/demo-chat-attachments/private%23part": (403, b"denied"),
            "/demo-chat-attachments/private": (200, b"prefix-object"),
            "/demo-chat-attachments/space%20key": (403, b"denied"),
            "/login": (200, b"login page"),
        }
        if self.path == "/demo-chat-attachments/redirected.txt":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        status, body = routes.get(self.path, (404, b"missing"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LoopbackProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_redirect_is_unknown_not_open(self):
        result = exposure.probe(self.endpoint, BUCKET, "redirected.txt")
        self.assertEqual(result["state_observed"], "unknown")
        self.assertTrue(result["anonymous_get"].startswith("UNKNOWN:REDIRECT_302"))

    def test_reserved_characters_reach_the_server_as_the_same_key(self):
        self.assertEqual(exposure.probe(self.endpoint, BUCKET, "private#part")["state_observed"], "closed")
        self.assertEqual(exposure.probe(self.endpoint, BUCKET, "space key")["state_observed"], "closed")
        self.assertEqual(exposure.probe(self.endpoint, BUCKET, "open.txt")["state_observed"], "open")
        self.assertEqual(exposure.probe(self.endpoint, BUCKET, "missing.txt")["anonymous_get"], "UNKNOWN:404")
