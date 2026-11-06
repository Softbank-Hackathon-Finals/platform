"""Regression: direct API access keeps streaming and credential boundaries."""

import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = (
    Path(__file__).resolve().parents[2] / "infra/envs/rehearsal-access/team_test.py"
)
SPEC = importlib.util.spec_from_file_location("team_test", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WrapperTests(unittest.TestCase):
    def test_requests_use_direct_api_and_preserve_streaming_choice(self):
        self.run_wrapper()

    def test_auth_proxy_as_direct_endpoint_never_starts_test(self):
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.run_wrapper(direct_identity="operator@example.test")

    def test_wrong_direct_cluster_never_starts_test(self):
        with self.assertRaisesRegex(RuntimeError, "direct API destination"):
            self.run_wrapper(direct_cluster="different-cluster")

    def run_wrapper(self, direct_identity=None, direct_cluster=None):
        settings = json.loads(SOURCE.with_name("team-test.example.json").read_text())
        subject = "system:serviceaccount:team-access:member-01"
        calls = []
        configs = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(settings))

            def run(command, **kwargs):
                calls.append(command)
                self.assertNotIn("port-forward", command)
                bootstrap = any(c.startswith("--kubeconfig=") for c in command)
                if not bootstrap:
                    cfg = json.loads(Path(kwargs["env"]["KUBECONFIG"]).read_text())
                    self.assertEqual(
                        cfg["clusters"][0]["cluster"],
                        {"server": settings["direct_server"]},
                    )
                    self.assertEqual(set(cfg["users"][0]["user"]), {"exec"})
                    configs.append(kwargs["env"]["KUBECONFIG"])
                if "whoami" in command:
                    user = (
                        "operator@example.test"
                        if bootstrap
                        else (direct_identity or subject)
                    )
                    output = json.dumps({"status": {"userInfo": {"username": user}}})
                elif "get" in command:
                    output = (
                        settings["cluster_uid"]
                        if bootstrap
                        else (direct_cluster or settings["cluster_uid"])
                    )
                else:
                    payload = base64.urlsafe_b64encode(
                        json.dumps({"sub": subject, "exp": time.time() + 600}).encode()
                    ).decode()
                    output = "test." + payload + ".fixture"
                return subprocess.CompletedProcess(command, 0, output, "")

            def supervise(command, env, deadline):
                self.assertEqual(command, ["test-command"])
                self.assertEqual(env["KUBECTL_REMOTE_COMMAND_WEBSOCKETS"], "true")
                self.assertGreater(deadline, time.monotonic())
                return 17

            with (
                patch.object(
                    sys,
                    "argv",
                    [str(SOURCE), "--profile", str(path), "--", "test-command"],
                ),
                patch.object(MODULE.subprocess, "run", side_effect=run),
                patch.object(MODULE, "supervise", side_effect=supervise) as child,
                patch.dict(os.environ, {"KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "true"}),
            ):
                try:
                    self.assertEqual(MODULE.main(), 17)
                    child.assert_called_once()
                except RuntimeError:
                    child.assert_not_called()
                    raise
                finally:
                    self.assertTrue(all(not Path(p).exists() for p in configs))


@unittest.skipUnless(os.name == "posix", "operator wrapper requires POSIX/WSL")
class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "effects"

    def test_expiry_stops_descendants_and_never_replays(self):
        # A descendant would record a late effect if only the immediate child died.
        late = f"import time; from pathlib import Path; time.sleep(1); Path({str(self.marker)!r}).write_text('late')"
        command = [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{late!r}]); time.sleep(5)"
            ),
        ]
        with self.assertRaisesRegex(RuntimeError, "window ended"):
            MODULE.supervise(command, os.environ.copy(), time.monotonic() + 0.4)
        time.sleep(1)
        self.assertFalse(self.marker.exists())

    def test_expired_credential_never_starts_command(self):
        with self.assertRaisesRegex(RuntimeError, "not started"):
            MODULE.supervise(
                [sys.executable, "-c", f"open({str(self.marker)!r},'w').close()"],
                os.environ.copy(),
                time.monotonic() - 1,
            )
        self.assertFalse(self.marker.exists())

    def test_expiry_stops_running_command(self):
        command = [
            sys.executable,
            "-c",
            f"import time; time.sleep(1); open({str(self.marker)!r},'w').close()",
        ]
        with self.assertRaisesRegex(RuntimeError, "window ended"):
            MODULE.supervise(command, os.environ.copy(), time.monotonic() + 0.2)
        time.sleep(1)
        self.assertFalse(self.marker.exists())

    def test_failed_test_keeps_exit_status(self):
        self.assertEqual(
            17,
            MODULE.supervise(
                [sys.executable, "-c", "raise SystemExit(17)"],
                os.environ.copy(),
                time.monotonic() + 3,
            ),
        )

    def test_direct_profile_rejects_auth_proxy_reuse(self):
        example = SOURCE.with_name("team-test.example.json")
        value = json.loads(example.read_text())
        value["direct_server"] = value["server"]
        path = Path(self.directory.name) / "profile.json"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "distinct"):
            MODULE.profile(path)

    def test_direct_profile_rejects_credentials_or_plain_http(self):
        for origin in (
            "http://api.example.test",
            "https://user:pass@api.example.test",
            "https://api.example.test/?token=x",
        ):
            with self.subTest(origin=origin):
                value = json.loads(
                    SOURCE.with_name("team-test.example.json").read_text()
                )
                value["direct_server"] = origin
                path = Path(self.directory.name) / "profile.json"
                path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "HTTPS"):
                    MODULE.profile(path)


if __name__ == "__main__":
    unittest.main()
