"""Demo exposure states for the target object store; no cluster or network in default discovery."""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
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
