import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("design_bump", SOURCE / "scripts/design_bump.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


class DesignBumpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (bump.DOC, bump.HISTORY):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((SOURCE / name).read_bytes())
        self.git("init", "-b", "develop")
        self.git("config", "user.name", "Example")
        self.git("config", "user.email", "example@example.invalid")
        self.git("add", "docs")
        self.git("commit", "-m", "fixture")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-ref", "refs/remotes/origin/develop", self.base)
        self.git("switch", "-c", "feature/T29-example")
        self.old = bump.version((self.root / bump.DOC).read_text())
        self.new = "v99.1"

    def git(self, *args):
        return bump.git(self.root, *args)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / "docs").rglob("*") if p.is_file()}

    def prepare(self):
        return bump.prepare(self.root, self.new, "origin/develop", "2026-11-03")

    def test_default_develop_archive_preserves_working_edits_and_git_state(self):
        original = (self.root / bump.DOC).read_bytes()
        with (self.root / bump.DOC).open("ab") as output:
            output.write(b"\n<!-- working body edit -->\n")
        refs = self.git("show-ref")
        index = self.git("ls-files", "--stage")
        old, commit = self.prepare()
        self.assertEqual((old, commit), (self.old, self.base))
        self.assertEqual((self.root / bump.HISTORY.parent / f"design-{old}.html").read_bytes(), original)
        self.assertIn("working body edit", (self.root / bump.DOC).read_text())
        self.assertEqual(bump.version((self.root / bump.DOC).read_text()), self.new)
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(self.git("ls-files", "--stage"), index)
        self.assertEqual(self.git("branch", "--show-current").strip(), b"feature/T29-example")
        # No origin URL is configured: network access is neither needed nor attempted.
        self.assertEqual(self.git("remote"), b"")

    def test_invalid_versions_leave_all_files_unchanged(self):
        before = self.files()
        for new in (self.old, "v0", "v01.4", "v1..2", "--help"):
            with self.subTest(new=new), self.assertRaises(ValueError):
                bump.prepare(self.root, new, "origin/develop", "2026-11-03")
            self.assertEqual(self.files(), before)

    def test_missing_history_or_footer_is_rejected_before_any_write(self):
        for file, text in ((bump.HISTORY, "broken table"), (bump.DOC, "<dt>상태</dt><dd>초안 " + self.old + "</dd>")):
            path = self.root / file
            original = path.read_bytes()
            path.write_text(text)
            before = self.files()
            with self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(self.files(), before)
            path.write_bytes(original)

    def test_repeated_prepare_preserves_archive_and_current_files(self):
        self.prepare()
        before = self.files()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.files(), before)

    def test_protected_or_detached_branch_is_rejected(self):
        for branch in ("develop", "--detach"):
            self.git("switch", branch)
            before = self.files()
            with self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(self.files(), before)

    def test_unknown_revision_leaves_files_unchanged(self):
        before = self.files()
        with self.assertRaises(subprocess.CalledProcessError):
            bump.prepare(self.root, self.new, "missing-base", "2026-11-03")
        self.assertEqual(self.files(), before)

    def test_failed_history_write_restores_original_files(self):
        before = self.files()
        write = Path.write_bytes
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if path == self.root / bump.HISTORY and not failed:
                failed = True
                raise OSError("synthetic write failure")
            return write(path, data)

        with patch.object(Path, "write_bytes", fail_once), self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
