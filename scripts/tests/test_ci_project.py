"""The project runner must preserve missing-check and test-failure outcomes."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "ci_project.py"


class ProjectChecksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "tool"
        self.project.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        # Stand-ins preserve tool exit semantics without fetching dependencies in
        # the stdlib-only scope job. Real uv/pytest is also checked before push.
        program = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
with open(os.environ['CHECK_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['run', '--locked', 'pytest']:
    if not Path('test_behavior.py').exists():
        sys.exit(5)
    if Path('fail-test').exists():
        sys.exit(1)
if sys.argv[1:] == ['run', 'test'] and Path('fail-test').exists():
    sys.exit(1)
"""
        for name in ("uv", "uvx", "pnpm"):
            executable = self.bin / name
            executable.write_text(program)
            executable.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], CHECK_LOG=str(self.log))

    def check(self, kind):
        return subprocess.run([sys.executable, str(RUNNER), "tool", kind], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=10, check=False)

    def test_deleting_last_python_test_fails_instead_of_skipping_pytest(self):
        (self.project / "uv.lock").touch()
        test = self.project / "test_behavior.py"
        test.touch()
        self.assertEqual(self.check("python").returncode, 0)
        test.unlink()
        missing = self.check("python")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("exit status 5", missing.stderr)

    def test_failed_python_tests_remain_failures(self):
        (self.project / "uv.lock").touch()
        (self.project / "test_behavior.py").touch()
        (self.project / "fail-test").touch()
        self.assertNotEqual(self.check("python").returncode, 0)

    def test_node_cannot_omit_or_empty_any_required_check(self):
        (self.project / "pnpm-lock.yaml").touch()
        for name in ("lint", "test", "build"):
            for value in (None, "", "  ", False):
                with self.subTest(name=name, value=value):
                    scripts = dict.fromkeys(("lint", "test", "build"), "fixture-command")
                    if value is None:
                        del scripts[name]
                    else:
                        scripts[name] = value
                    (self.project / "package.json").write_text(json.dumps({"scripts": scripts}))
                    self.assertNotEqual(self.check("node").returncode, 0)
                    self.assertFalse(self.log.exists(), "invalid contracts must fail before install")

    def test_node_test_failure_stops_before_build(self):
        (self.project / "pnpm-lock.yaml").touch()
        (self.project / "package.json").write_text(json.dumps({"scripts": dict.fromkeys(("lint", "test", "build"), "fixture-command")}))
        (self.project / "fail-test").touch()
        self.assertNotEqual(self.check("node").returncode, 0)
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertNotIn(["run", "build"], commands)
