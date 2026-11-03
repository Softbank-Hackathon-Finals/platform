import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("public_evidence", Path(__file__).parents[1] / "public_evidence.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


@contextlib.contextmanager
def evidence_fixture(body):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo, private = root / "repo", root / "private"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
        name = "docs/evidence/probe.json"
        path = repo / name
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(body) + "\n")
        yield repo, private, name, path


class PublicEvidence(unittest.TestCase):
    def test_github_ssh_transport_is_not_a_personal_email_allowlist(self):
        transport = "git" + "@github.com"
        person = "operator" + "@github.com"
        neighbor = "person" + "@private-mail.org"
        for remote in (transport + ":owner/repo.git", "ssh://" + transport + "/owner/repo",
                       transport + ":{REPO}"):
            with self.subTest(remote=remote):
                self.assertFalse(privacy.findings(remote, "scripts/tool.py"))
                found = privacy.findings(remote + " " + neighbor + " " + person, "scripts/tool.py")
                self.assertEqual(found, {("EMAIL", neighbor), ("EMAIL", person)})
        for address in (transport, person + ":owner/repo", "ssh://" + person + "/owner/repo", transport + ": no-path"):
            with self.subTest(address=address):
                self.assertTrue(privacy.findings(address, "scripts/tool.py"))

    def test_sanitization_keeps_transport_when_same_address_is_also_an_email(self):
        transport = "git" + "@github.com"
        body = {"remote": "ssh://" + transport + "/owner/repo", "email": transport}
        with evidence_fixture(body) as (repo, private, name, path):
            with contextlib.redirect_stdout(io.StringIO()):
                privacy.sanitize(repo, [name], private, "manifest.json")
            public = json.loads(path.read_text())
            self.assertEqual(public["remote"], body["remote"])
            self.assertNotEqual(public["email"], body["email"])
            self.assertFalse(privacy.findings(path.read_text(), name))

    def test_example_prefix_does_not_exempt_real_email_or_cloud_endpoint(self):
        for value, kind in (("example-operator@" + "mail.org", "EMAIL"),
                            ("AWS_operator@" + "mail.org", "EMAIL"),
                            ("example-private." + "rds.amazonaws.com", "ENDPOINT")):
            with self.subTest(kind=kind):
                self.assertIn((kind, value), privacy.findings(value, "README.md"))

    def test_github_ssh_transport_is_not_a_personal_email(self):
        self.assertFalse(privacy.findings(
            "git@github.com:team/repo.git ssh://git@github.com/team/repo", "script.py"))

    def test_resource_names_cannot_change_hashes_verdicts_or_json_keys(self):
        for resource_name in ("db", "PASS", "db_count"):
            with self.subTest(resource_name=resource_name):
                body = {"GroupName": resource_name, "source_sha256": "db" + "a" * 62,
                        "decision": "PASS", "status": "PASS", "db_count": 17,
                        "db": "db", "RESOURCE_NAME_001": "original-key",
                        "note": "db db_schema PASS", "same_name": resource_name,
                        "metrics": {"label": resource_name, "elapsed_ms": 19}}
                with evidence_fixture(body) as (repo, private, name, path):
                    with contextlib.redirect_stdout(io.StringIO()):
                        privacy.sanitize(repo, [name], private, "manifest.json")
                    public = json.loads(path.read_text())
                    self.assertTrue(public.pop("GroupName").startswith("RESOURCE_NAME_"))
                    self.assertEqual(public, {k: v for k, v in body.items() if k != "GroupName"})
                    self.assertFalse(privacy.findings(path.read_text(), name))
                    self.assertEqual(json.loads(next(private.glob("*/docs/evidence/probe.json")).read_text()), body)

    def test_resource_name_locations_include_lists_and_name_tags_only(self):
        body = {"GroupName": "db", "log_groups": ["db", "db-two"],
                "Tags": [{"Key": "Name", "Value": "db"}, {"Key": "role", "Value": "db"}]}
        with evidence_fixture(body) as (repo, private, name, path):
            with contextlib.redirect_stdout(io.StringIO()):
                privacy.sanitize(repo, [name], private, "manifest.json")
            public = json.loads(path.read_text())
            self.assertEqual(public["GroupName"], public["log_groups"][0])
            self.assertEqual(public["GroupName"], public["Tags"][0]["Value"])
            self.assertEqual(public["Tags"][1], body["Tags"][1])
            self.assertEqual([tag["Key"] for tag in public["Tags"]], ["Name", "role"])
            self.assertFalse(privacy.findings(path.read_text(), name))

    def test_generic_identifiers_replace_only_detected_token_boundaries(self):
        resource = "sg-" + "ab" * 8 + "c"
        body = {"resource": resource, "message": "group " + resource + " ready",
                "not_a_token": "prefix" + resource + "suffix", "GroupName": "db"}
        with evidence_fixture(body) as (repo, private, name, path):
            with contextlib.redirect_stdout(io.StringIO()):
                privacy.sanitize(repo, [name], private, "manifest.json")
            public = json.loads(path.read_text())
            self.assertEqual(public["message"], "group " + public["resource"] + " ready")
            self.assertEqual(public["not_a_token"], body["not_a_token"])

    def test_preservation_guard_stops_corrupt_rewriter_before_any_public_write(self):
        mutations = [
            lambda value: value.update(source_sha256="RESOURCE_NAME_001" + "a" * 62),
            lambda value: value.update(decision="RESOURCE_NAME_001"),
            lambda value: value.update(db_count=18),
            lambda value: value.update(units=3.0),  # Type changes are not measurement preservation.
            lambda value: value.update(db_count=value.pop("other_count")),
            lambda value: value["metrics"].update(label="RESOURCE_NAME_001"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                body = {"GroupName": "db", "source_sha256": "db" + "a" * 62, "decision": "PASS",
                        "db_count": 17, "other_count": 17, "units": 3, "metrics": {"label": "db"}}
                with evidence_fixture(body) as (repo, private, name, path):
                    original = path.read_bytes()
                    real_rewrite = privacy.rewrite_json

                    def corrupt(value, *args, _rewrite=real_rewrite, _mutate=mutate, **kwargs):
                        # Only corrupt the top-level result, including a shared protected subtree.
                        result = _rewrite(value, *args, **kwargs)
                        if isinstance(result, dict) and "GroupName" in result:
                            _mutate(result)
                        return result

                    with patch.object(privacy, "rewrite_json", side_effect=corrupt), self.assertRaises(ValueError):
                        privacy.sanitize(repo, [name], private, "manifest.json")
                    self.assertEqual(path.read_bytes(), original)
                    self.assertFalse((repo / "manifest.json").exists())
                    self.assertFalse(private.exists())

    def test_ambiguous_numeric_measurement_is_not_rewritten_as_an_account(self):
        account = int("9" + "0" * 10 + "1")
        body = {"account": account, "GroupName": "db", "measured_value": account}
        with evidence_fixture(body) as (repo, private, name, path):
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                privacy.sanitize(repo, [name], private, "manifest.json")
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((repo / "manifest.json").exists())

    def test_private_identifier_in_json_key_requires_manual_review(self):
        resource = "sg-" + "ab" * 8 + "c"
        with evidence_fixture({resource: 17}) as (repo, private, name, path):
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                privacy.sanitize(repo, [name], private, "manifest.json")
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((repo / "manifest.json").exists())

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

    def test_cli_fails_closed_without_private_paths_or_input_in_errors(self):
        script = Path(privacy.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            deleted = repo / "deleted.md"
            deleted.write_text("example")
            subprocess.run(["git", "-C", str(repo), "add", "deleted.md"], check=True)
            deleted.unlink()
            raw_value = "operator@" + "private-mail.org"
            malformed = repo / "docs/evidence/invalid.json"
            malformed.parent.mkdir(parents=True)
            malformed.write_text('{"private": "' + raw_value + '",')
            cases = [
                ["check", "--repo", str(repo)],  # Tracked, then deleted without staging.
                ["check", "missing.md", "--repo", str(repo)],
                ["check", "docs/evidence/invalid.json", "--repo", str(repo)],
                ["check", "--repo", str(repo / "missing-repository")],
            ]
            commands = [(args, None) for args in cases]
            commands.append((["check", "--repo", str(repo)], {**os.environ, "PATH": ""}))
            for args, env in commands:
                with self.subTest(args=args[:2], missing_git=env is not None):
                    result = subprocess.run([sys.executable, str(script), *args],
                                            capture_output=True, text=True, check=False, env=env)
                    self.assertEqual(result.returncode, 2)
                    output = result.stdout + result.stderr
                    self.assertIn("validation failed", output)
                    for forbidden in (str(repo), str(script), str(Path.home()), raw_value, "Traceback"):
                        self.assertNotIn(forbidden, output)
