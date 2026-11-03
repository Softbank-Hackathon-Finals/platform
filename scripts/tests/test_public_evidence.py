import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_evidence", Path(__file__).parents[1] / "public_evidence.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class PublicEvidence(unittest.TestCase):
    def test_flags_identifiers_without_confusing_hashes_or_svg_coordinates(self):
        account = "9" + "0" * 10 + "1"
        resource = "i-" + "ab" * 8 + "c"
        email = "operator@" + "mail.org"
        raw = f'{account} {resource} {email} ' + '/Users/' + 'private-user/work'
        kinds = {kind for kind, _ in privacy.findings(raw, "docs/evidence/example.md")}
        self.assertEqual(kinds, {"AWS_ACCOUNT", "AWS_RESOURCE", "EMAIL", "HOME"})
        self.assertFalse(privacy.findings("a" + account + "b 1." + account + "4", "uv.lock"))

    def test_permits_documented_examples_and_existing_masked_provider_arns(self):
        examples = "123456789012 owner@example.invalid 192.0.2.10 arn:aws:iam::aws:policy/ReadOnlyAccess"
        self.assertFalse(privacy.findings(examples, "docs/evidence/example.md"))
        self.assertTrue(privacy.permitted("AWS_ARN", "arn:aws:securityhub:region:1111****:hub/default"))
        self.assertTrue(privacy.permitted("AWS_ARN", "arn:aws:securityhub:region::product/aws/securityhub"))

    def test_operational_resource_names_and_host_ips_are_evidence_only(self):
        content = json.dumps({"GroupName": "private-cloud-group", "host": "10.10.1.9",
                              "cidr": "10.10.0.0/16"})
        found = privacy.findings(content, "docs/evidence/result.json")
        self.assertIn(("RESOURCE_NAME", "private-cloud-group"), found)
        self.assertIn(("IP", "10.10.1.9"), found)
        self.assertNotIn(("IP", "10.10.0.0"), found)
        self.assertFalse(privacy.findings(content, "services/tests/fixture.json"))

    def test_sanitization_preserves_raw_bytes_and_embedded_measurement_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, private = root / "repo", root / "private"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
            name = "docs/evidence/runtime.json"
            path = repo / name
            path.parent.mkdir(parents=True)
            account = "9" + "0" * 10 + "1"
            resource = "i-" + "ab" * 8 + "c"
            body = {"account": int(account), "resource": resource, "resource_again": resource,
                    "source_sha256": "c" * 64, "passed": 74}
            original = (json.dumps(body, indent=2).replace("\n", "\r\n") + "\r\n").encode()
            path.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()):
                privacy.sanitize(repo, [name], private, "docs/evidence/privacy/manifest.json")
            public = json.loads(path.read_text())
            self.assertEqual(public["passed"], 74)
            self.assertEqual(public["source_sha256"], body["source_sha256"])
            self.assertEqual(public["resource"], public["resource_again"])
            self.assertFalse(privacy.findings(path.read_text(), name))
            manifest = json.loads((repo / "docs/evidence/privacy/manifest.json").read_text())
            entry = manifest["runs"][0]["artifacts"][0]
            self.assertEqual(entry["original_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(entry["public_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            backup = next(private.glob("*/docs/evidence/runtime.json"))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(account, json.dumps(manifest))
            self.assertNotIn(str(private), json.dumps(manifest))
            with contextlib.redirect_stdout(io.StringIO()):
                privacy.sanitize(repo, [name], private, "docs/evidence/privacy/manifest.json")
            self.assertEqual(manifest, json.loads((repo / "docs/evidence/privacy/manifest.json").read_text()))

    def test_private_backup_inside_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            with self.assertRaisesRegex(ValueError, "outside repository"):
                privacy.sanitize(repo, [], repo / "private", "manifest.json")

    def test_checker_reports_types_not_values_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            value = "person@" + "mail.org"
            (repo / "note.md").write_text(value)
            os.symlink("note.md", repo / "alias.md")
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                self.assertEqual(privacy.check(repo, ["note.md", "alias.md"]), 1)
            self.assertIn("EMAIL", capture.getvalue())
            self.assertNotIn(value, capture.getvalue())
            self.assertEqual(privacy.read_text(repo, "alias.md"), "note.md")

    def test_rejects_absolute_paths_and_parent_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            path = repo / "note.md"
            path.write_text("example")
            for name in (str(path), "../outside", "nested/../note.md"):
                with self.assertRaisesRegex(ValueError, "repository-relative"):
                    privacy.read_text(repo, name)

    def test_skip_boundary_for_binary_and_large_files_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "binary.bin").write_bytes(b"\xff\xfe")
            (repo / "large.txt").write_bytes(b"a" * (4 * 1024 * 1024 + 1))
            self.assertIsNone(privacy.read_text(repo, "binary.bin"))
            self.assertIsNone(privacy.read_text(repo, "large.txt"))
