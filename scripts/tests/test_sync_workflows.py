"""Guard the code/credential boundary and error propagation of board automation."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


class SyncWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.sync = (WORKFLOWS / "sync-tasks.yml").read_text()

    def test_privileged_workflow_is_not_loaded_from_a_review_merge_commit(self):
        self.assertNotRegex(self.sync, r"(?m)^  pull_request(?:_review|_review_comment)?:")
        self.assertRegex(self.sync, r"(?m)^  workflow_run:\n    workflows: \[pr-review-signal\]")
        self.assertIn("github.event.workflow_run.event == 'pull_request_review'", self.sync)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.sync)
        self.assertNotIn("github.event.workflow_run.head_sha", self.sync)
        self.assertNotIn("download-artifact", self.sync)

    def test_script_revision_is_bound_to_the_workflow_not_a_moving_branch(self):
        refs = re.findall(r"(?m)^\s+ref: (.+)$", self.sync)
        self.assertEqual(refs, ["${{ github.workflow_sha }}"])
        self.assertIn("persist-credentials: false", self.sync)

    def test_review_signal_cannot_receive_credentials_or_execute_repository_code(self):
        signal = (WORKFLOWS / "pr-review-signal.yml").read_text()
        self.assertRegex(signal, r"(?m)^  pull_request_review:")
        self.assertRegex(signal, r"(?m)^permissions: \{\}$")
        for forbidden in ("secrets.", "uses:", "checkout", "github.event.", "upload-artifact"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, signal)

    def test_sync_command_failure_is_not_hidden_by_summary_or_followup(self):
        block = re.search(
            r"id: sync\n(?:(?!      - name:).)*?        run: \|\n"
            r"(?P<body>(?:          [^\n]*(?:\n|$))+)", self.sync, re.DOTALL,
        )
        self.assertIsNotNone(block)
        script = "\n".join(line[10:] for line in block["body"].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python3"
            env = {**os.environ, "PATH": f"{root}:{os.defpath}", "COMMAND": "prs",
                   "DRY": "--dry-run", "GITHUB_STEP_SUMMARY": str(root / "summary")}
            for code in (0, 1, 2, 124):
                with self.subTest(exit_code=code):
                    python.write_text(f"#!/bin/sh\nprintf 'sync result\\n'\nexit {code}\n")
                    python.chmod(0o700)
                    (root / "summary").write_text("")
                    result = subprocess.run(
                        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                        cwd=root, env=env, capture_output=True, text=True, timeout=10, check=False,
                    )
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual((root / "summary").read_text(), "sync result\n")

    def test_start_task_is_an_argument_and_rejects_shell_text_before_running(self):
        block = re.search(
            r"id: sync\n(?:(?!      - name:).)*?        run: \|\n"
            r"(?P<body>(?:          [^\n]*(?:\n|$))+)", self.sync, re.DOTALL,
        )
        script = "\n".join(line[10:] for line in block["body"].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "python3"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > invoked\n")
            executable.chmod(0o700)
            env = {**os.environ, "PATH": f"{root}:{os.defpath}", "COMMAND": "start",
                   "DRY": "--dry-run", "GITHUB_STEP_SUMMARY": str(root / "summary")}
            for task in ("", "T23; touch injected", "$(touch injected)", "--help", "T23 T24"):
                with self.subTest(task=task):
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                        env={**env, "TASK": task}, capture_output=True, timeout=10, check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse((root / "invoked").exists())
                    self.assertFalse((root / "injected").exists())
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                env={**env, "TASK": "T23"}, capture_output=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "invoked").read_text().splitlines(),
                             ["scripts/sync_tasks.py", "start", "T23", "--dry-run", "--moves-file", "moves.txt"])

    def test_activity_has_a_scoped_task_and_rejects_invalid_intent_before_running(self):
        block = re.search(
            r"id: sync\n(?:(?!      - name:).)*?        run: \|\n"
            r"(?P<body>(?:          [^\n]*(?:\n|$))+)", self.sync, re.DOTALL,
        )
        script = "\n".join(line[10:] for line in block["body"].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "python3"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > invoked\n")
            executable.chmod(0o700)
            env = {**os.environ, "PATH": f"{root}:{os.defpath}", "COMMAND": "activity",
                   "DRY": "--dry-run", "GITHUB_STEP_SUMMARY": str(root / "summary")}
            for task, intent in (("", "active"), ("T30; touch injected", "active"),
                                 ("T30", ""), ("T30", "active; touch injected"),
                                 ("T30", "$(touch injected)"), ("T30", "--help")):
                with self.subTest(task=task, intent=intent):
                    result = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                        env={**env, "TASK": task, "ACTIVITY": intent}, capture_output=True, timeout=10, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse((root / "invoked").exists())
                    self.assertFalse((root / "injected").exists())
            for intent in ("active", "auto"):
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script], cwd=root,
                    env={**env, "TASK": "T30", "ACTIVITY": intent}, capture_output=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "invoked").read_text().splitlines(),
                                 ["scripts/sync_tasks.py", "activity", "T30", intent,
                                  "--dry-run", "--moves-file", "moves.txt"])


if __name__ == "__main__":
    unittest.main()
