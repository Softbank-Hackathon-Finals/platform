"""Bootstrap Helm calls: settled-release guard and server-side apply ownership."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bootstrap

REVISION = "a" * 40


def fake(status, catalog=None):
    calls = []

    def run(args, *, check=True):
        calls.append(args)
        out = ""
        if args[-2:] == ["rev-parse", "HEAD"]:
            out = REVISION
        elif "version" in args:
            out = "v4.0.0"
        elif "get" in args and "configmap" in args:
            out = json.dumps({"data": {"catalog.json": json.dumps(catalog)}})
        elif "get" in args:
            out = json.dumps({"metadata": {"uid": "cluster" if "namespace" in args else "deploy"}})
        elif "list" in args:
            out = json.dumps([{"status": status}])
        elif "status" in args and "-o" in args:
            out = json.dumps({"info": {"status": "deployed"}, "version": 5})
        return SimpleNamespace(stdout=out, returncode=0)
    return run, calls


def main(status, catalog=None):
    run, calls = fake(status, catalog)
    argv = ["bootstrap.py", "--cluster-uid", "cluster", "--deployment-uid", "deploy",
            "--revision", REVISION, "--kubeconfig", "/k", "--apply"]
    with tempfile.TemporaryDirectory() as d:
        if catalog is not None:
            values = Path(d) / "runtime.json"
            values.write_text(json.dumps({"registration": {"enabled": True, "runtime": {
                "configuration": {"existingConfigMap": "cfg", "sha256": "0" * 64},
                "secrets": {"database": "db"}}}}))
            argv += ["--runtime-values", str(values)]
        with mock.patch.object(bootstrap, "run", run), mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print"):
            bootstrap.main()
    return calls


def profiles(*ids):
    return {"evaluation_profiles": [{"tenant_id": "bravo", "profile_id": i, "profile_version": "1"} for i in ids]}


class InitialProposal(unittest.TestCase):
    PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"

    def test_host_creates_jobs_is_an_optional_bool(self):
        seen = []
        get = lambda *a: seen.append(a)
        for value in ({"enabled": True, "brokerCaPem": self.PEM},
                      {"enabled": True, "brokerCaPem": self.PEM, "hostCreatesJobs": True}):
            bootstrap.verify_initial_proposal(get, value)
        self.assertEqual(seen[-1], ("secret", "platform-ghcr-reader", "bravo-initial"))
        for value in ({"enabled": True, "brokerCaPem": self.PEM, "hostCreatesJobs": "yes"},
                      {"enabled": True, "brokerCaPem": self.PEM, "other": 1},
                      {"enabled": True, "brokerCaPem": self.PEM + "PRIVATE KEY"}, {"brokerCaPem": self.PEM}):
            with self.assertRaises(ValueError):
                bootstrap.verify_initial_proposal(get, value)


class BootstrapHelm(unittest.TestCase):
    def test_upgrade_takes_back_its_own_fields(self):
        upgrade = next(c for c in main("deployed") if "upgrade" in c)
        self.assertIn("--force-conflicts", upgrade)
        self.assertNotIn("--force", upgrade)

    def test_failed_release_is_recovered_by_upgrade(self):
        self.assertTrue(any("upgrade" in c for c in main("failed")))

    def test_pending_release_is_never_touched(self):
        for status in ("pending-upgrade", "pending-install", "pending-rollback", "uninstalling"):
            with self.assertRaises(ValueError):
                main(status)

    def test_catalog_profiles_match_the_declared_allowlist(self):
        self.assertTrue(any("upgrade" in c for c in main("deployed", profiles("apply-recheck", "demo-gate"))))
        for drift in (profiles("full", "demo-gate", "apply-recheck"), profiles("demo-gate"), {}):
            with self.assertRaisesRegex(ValueError, "registration_profiles_drift"):
                main("deployed", drift)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    unittest.main()
