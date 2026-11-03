"""Start/status must not turn implementation progress into completion."""

import contextlib
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sync = importlib.import_module("sync_tasks")


class BoardCommandsTest(unittest.TestCase):
    def setUp(self):
        self.task = sync.Task("T1", "Foundation", "1", prio="P0")
        self.issues = {"T1": {
            "id": "issue-1", "number": 5, "state": "OPEN",
            "body": "- [ ] network\n- [ ] budget\n",
            "assignees": [{"login": "mangowhoiscloud"}],
        }}
        self.board = Mock()
        self.board.status.return_value = "Backlog"
        for name, value in (("load_issues", self.issues), ("Board", self.board), ("parse", {"T1": self.task})):
            p = patch.object(sync, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(Path, "read_text", return_value="plan")
        p.start()
        self.addCleanup(p.stop)
        self.mutate = patch.object(sync, "run").start()
        self.addCleanup(patch.stopall)

    def test_start_marks_progress_without_checking_or_closing(self):
        sync.start("T1", False)
        self.board.set.assert_called_once_with(5, "issue-1", "In progress", "T1 #5", False)
        self.mutate.assert_not_called()
        self.assertNotIn("[x]", self.issues["T1"]["body"])

    def test_start_refuses_closed_or_reviewed_work(self):
        self.issues["T1"]["state"] = "CLOSED"
        with self.assertRaises(ValueError):
            sync.start("T1", False)
        self.issues["T1"]["state"] = "OPEN"
        self.board.status.return_value = "In review"
        with self.assertRaises(ValueError):
            sync.start("T1", False)
        self.board.set.assert_not_called()

    def test_start_requires_prerequisite_but_not_integration_task(self):
        self.task.deps = ["T2"]
        with self.assertRaisesRegex(ValueError, "선행 미완료"):
            sync.start("T1", False)
        self.task.deps = []
        self.task.deps_int = ["T2"]
        sync.start("T1", True)
        self.board.set.assert_called_once_with(5, "issue-1", "In progress", "T1 #5", True)

    def test_status_is_read_only_and_distinguishes_dependencies(self):
        self.task.deps, self.task.deps_int = ["T2"], ["T3"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sync.status()
        row = json.loads(out.getvalue())["tasks"][0]
        self.assertEqual(row["blocked_by"], ["T2"])
        self.assertEqual(row["integration_pending"], ["T3"])
        self.assertEqual((row["checked"], row["total"]), (0, 2))
        self.board.set.assert_not_called()
        self.mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
