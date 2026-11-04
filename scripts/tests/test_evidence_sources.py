"""Source hashes retain their bytes and immutable revision across publication."""

import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sources = importlib.import_module("evidence_sources")


class SourceEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "api.py").write_bytes(b"source at measurement\n")
        self.evidence = self.repo / "docs/evidence/run.json"
        self.evidence.parent.mkdir(parents=True)
        self.old = {"source_sha256": {"api.py": hashlib.sha256(b"historic source").hexdigest()}, "passed": 3}
        self.evidence.write_text(json.dumps(self.old))
        self.git("add", ".")
        self.git("commit", "-m", "historical input")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def test_generator_hashes_requested_committed_bytes_not_dirty_worktree(self):
        (self.repo / "api.py").write_bytes(b"different unmeasured bytes\n")
        record = sources.create(self.repo, self.base, ["api.py"])
        self.assertEqual(record, {"source_revision": self.base, "source_files": [
            {"path": "api.py", "sha256": hashlib.sha256(b"source at measurement\n").hexdigest()}]})
        self.assertEqual(sources.format_errors(record), [])

    def test_generator_rejects_missing_symlink_duplicate_and_escape(self):
        (self.repo / "alias.py").symlink_to("api.py")
        self.git("add", "alias.py")
        self.git("commit", "-m", "symlink")
        head = self.git("rev-parse", "HEAD")
        for paths in (["missing.py"], ["alias.py"], ["api.py", "api.py"], ["../api.py"], ["/api.py"]):
            with self.subTest(paths=paths), self.assertRaises((ValueError, subprocess.SubprocessError)):
                sources.create(self.repo, head, paths)

    def test_unchanged_historical_subtree_preserved_but_new_hash_map_rejected(self):
        original = self.evidence.read_bytes()
        self.assertEqual(sources.check(self.repo, self.base), 0)
        self.assertEqual(self.evidence.read_bytes(), original)
        value = dict(self.old, passed=4)
        self.evidence.write_text(json.dumps(value))
        self.assertEqual(sources.check(self.repo, self.base), 0)
        value["source_sha256"] = {"api.py": hashlib.sha256(b"new measured source").hexdigest()}
        self.evidence.write_text(json.dumps(value))
        self.assertEqual(sources.check(self.repo, self.base), 1)

    def test_new_file_cannot_copy_legacy_layout_but_canonical_rows_pass(self):
        new = self.evidence.with_name("new.json")
        new.write_text(json.dumps(self.old))
        self.git("add", str(new))
        self.assertEqual(sources.check(self.repo, self.base), 1)
        new.write_text(json.dumps(sources.create(self.repo, self.base, ["api.py"]), indent=2))
        self.assertEqual(sources.check(self.repo, self.base), 0)

    def test_nested_legacy_records_and_new_paths_are_checked(self):
        previous = {"phases": [self.old]}
        self.assertEqual(sources.format_errors(previous, previous), [])
        changed = {"phases": [self.old, self.old]}
        self.assertTrue(sources.format_errors(changed, previous))
        copied = {"other": self.old}
        self.assertTrue(sources.format_errors(copied, previous))
        self.assertTrue(sources.format_errors({"api.py": "A" * 64}))

    def test_historical_source_file_count_is_not_reinterpreted_as_a_manifest(self):
        previous = {"source_files": 32, "passed": True}
        self.assertEqual(sources.format_errors(dict(previous, passed=False), previous), [])
        self.assertTrue(sources.format_errors(previous))

    def test_malformed_canonical_rows_fail(self):
        row = {"path": "api.py", "sha256": "a" * 64}
        for rows in ([], {}, [row, row], [dict(row, path="../api.py")], [dict(row, sha256="broken")],
                     [dict(row, path=["api.py"])], [dict(row, extra=True)]):
            with self.subTest(rows=rows):
                self.assertTrue(sources.format_errors({"source_files": rows}))

    def test_unavailable_base_and_invalid_json_exit_two_without_original_error(self):
        for base, content in (("f" * 40, "{}"), (self.base, "invalid private document")):
            self.evidence.write_text(content)
            result = subprocess.run([sys.executable, sources.__file__, "--repo", str(self.repo),
                                     "check", "--base", base], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("private document", result.stderr)
            self.assertNotIn(str(self.repo), result.stderr)

    def test_evidence_symlink_is_not_followed(self):
        self.evidence.unlink()
        self.evidence.symlink_to(self.repo / "api.py")
        with self.assertRaises(ValueError):
            sources.check(self.repo, self.base)


if __name__ == "__main__":
    unittest.main()
