"""Real Git histories distinguish a PR's ancestry from unrelated fetched refs."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
scanner = importlib.import_module("ci_secrets")
sources = importlib.import_module("evidence_sources")
SCRIPT = Path(scanner.__file__).resolve()
REPO = SCRIPT.parents[1]


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
        self.commit("initial")
        self.receipt = Path(self.temp.name) / "receipt.json"

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_scan(self, scope, native):
        command = [sys.executable, str(SCRIPT), "--scope", scope, "--repo", str(self.repo), "--receipt", str(self.receipt)]
        if native != "docker":
            command += ["--native", native]
        return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)


class ExitBoundaryTest(GitFixture):
    def test_official_container_version_prefix_and_unavailable_scanner(self):
        binary = Path(self.temp.name) / "scanner"
        binary.write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1] == 'version':\n print('v8.24.3')\n")
        binary.chmod(0o700)
        self.assertEqual(self.run_scan("head", str(binary)).returncode, 0)
        self.assertEqual(json.loads(self.receipt.read_text())["scanner_version"], scanner.VERSION)
        binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(125)\n")
        result = self.run_scan("head", str(binary))
        self.assertEqual(result.returncode, 2)
        self.assertIn("preparation command failed (exit 125)", result.stderr)
        self.assertFalse(self.receipt.exists())

    def test_scanner_failures_are_not_hidden_by_receipt_write(self):
        binary = Path(self.temp.name) / "scanner"
        for code in (0, 1, 2, 124):
            with self.subTest(code=code):
                binary.write_text(f"#!{sys.executable}\nimport sys\nif sys.argv[1] == 'version':\n print('8.24.3')\nelse:\n sys.exit({code})\n")
                binary.chmod(0o700)
                result = self.run_scan("head", str(binary))
                self.assertEqual(result.returncode, code, result.stderr)
                record = json.loads(self.receipt.read_text())
                self.assertEqual(record["exit_code"], code)
                self.assertEqual(record["status"] == "PASS", code == 0)

    def test_missing_or_shallow_history_and_wrong_version_fail_closed(self):
        binary = Path(self.temp.name) / "scanner"
        binary.write_text("#!/bin/sh\necho 0.0.0\n")
        binary.chmod(0o700)
        self.assertEqual(self.run_scan("head", str(binary)).returncode, 2)
        self.assertFalse(self.receipt.exists())
        (self.repo / ".git/shallow").write_text(self.git("rev-parse", "HEAD") + "\n")
        self.assertEqual(self.run_scan("head", "missing-scanner").returncode, 2)
        self.assertFalse(self.receipt.exists())

    def test_audit_is_independent_and_scanner_is_mandatory_in_pr_gate(self):
        workflow = (REPO / ".github/workflows/ci.yml").read_text()
        audit = (REPO / ".github/workflows/security-history.yml").read_text()
        gate = workflow.split("  secrets:\n", 1)[1].split("  infra:\n", 1)[0]
        self.assertIn("ci_secrets.py --scope head", gate)
        self.assertNotIn("--scope repository", workflow)
        self.assertNotIn("history-secrets", workflow)
        self.assertIn("BRAVO_TEST_GITLEAKS: docker", gate)
        self.assertIn("evidence_sources.py check", gate)
        self.assertIn("ci_secrets.py --scope repository", audit)
        self.assertIn("fetch-depth: 0", audit)
        self.assertIn("schedule:", audit)
        self.assertIn("workflow_dispatch:", audit)
        self.assertIn("branches: [main, develop]", audit)
        self.assertIn("pull_request:\n    paths:", audit)
        for content in (gate, audit):
            self.assertNotIn("continue-on-error", content)
            self.assertNotIn("|| true", content)
        self.assertIn("contents: read", audit)
        self.assertNotIn("${{ secrets.", audit)


@unittest.skipUnless(os.getenv("BRAVO_TEST_GITLEAKS"), "set BRAVO_TEST_GITLEAKS to docker or CI-matching native binary")
class NativeScannerTest(GitFixture):
    def scan(self, scope, expected):
        result = self.run_scan(scope, os.environ["BRAVO_TEST_GITLEAKS"])
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(self.receipt.read_text())

    def secret(self):
        # Synthetic detector positive control, generated only in temporary repos.
        value = hashlib.sha256(b"synthetic regression data, not a credential").hexdigest()
        (self.repo / "config.txt").write_text("api_key = '" + value + "'\n")

    def test_unrelated_branch_and_tag_fail_audit_but_not_pr(self):
        self.git("checkout", "-b", "unrelated")
        self.secret()
        secret_commit = self.commit("synthetic positive control")
        self.git("tag", "historical-control")
        self.git("checkout", "main")
        self.git("checkout", "-b", "pr")
        (self.repo / "readme.txt").write_text("safe change\n")
        self.commit("PR")
        record = self.scan("head", 0)
        self.assertEqual(record["fetched_refs"], [])
        audit = self.scan("repository", 1)
        self.assertTrue(any(secret_commit in ref for ref in audit["fetched_refs"]))
        self.git("branch", "-D", "unrelated")
        self.scan("repository", 1)  # A tag alone still retains the full-history finding.
        self.scan("head", 0)

    def test_deleted_secret_in_own_ancestry_still_blocks(self):
        self.secret()
        self.commit("synthetic positive control")
        (self.repo / "config.txt").unlink()
        self.commit("remove current file")
        self.scan("head", 1)
        self.scan("repository", 1)

    def test_secret_created_only_by_merge_resolution_blocks(self):
        self.git("checkout", "-b", "side")
        (self.repo / "side.txt").write_text("safe\n")
        self.commit("side")
        self.git("checkout", "main")
        (self.repo / "main.txt").write_text("safe\n")
        self.commit("main")
        self.git("merge", "--no-commit", "--no-ff", "side")
        self.secret()
        self.commit("merge resolution positive control")
        self.scan("head", 1)

    def test_canonical_digest_rows_pass_where_filename_keys_fail(self):
        (self.repo / "plan_api.py").write_text("print('probe')\n")
        commit = self.commit("source")
        canonical = sources.create(self.repo, commit, ["plan_api.py"])
        (self.repo / "evidence.json").write_text(json.dumps(canonical, indent=2) + "\n")
        self.commit("canonical source digest")
        self.scan("head", 0)
        (self.repo / "old.json").write_text(json.dumps({"plan_api.py": canonical["source_files"][0]["sha256"]}, indent=2))
        self.commit("original incident shape")
        self.scan("head", 1)


if __name__ == "__main__":
    unittest.main()
