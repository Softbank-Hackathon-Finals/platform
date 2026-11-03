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
        self.board.pr_states = {}
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


class PRBoardTest(unittest.TestCase):
    def test_reconcile_recovers_plan_issue_and_pr_events_in_order(self):
        calls = Mock()
        with patch.object(sync, 'push', calls.push), patch.object(sync, 'pull', calls.pull), \
             patch.object(sync, 'sync_prs', calls.prs):
            sync.reconcile(True)
        self.assertEqual([c[0] for c in calls.mock_calls], ['push', 'pull', 'prs'])
        for c in calls.mock_calls:
            self.assertEqual(c.args, (True,))

    def issue(self, body='- [x] network\n- [ ] budget'):
        return {'state': 'OPEN', 'body': body}

    def pr(self, **updates):
        return {'number': 73, 'title': '[T29] workflow', 'headRefName': 'feature/T29-board', 'baseRefName': 'develop',
                'state': 'OPEN', 'isDraft': False, 'reviewDecision': 'REVIEW_REQUIRED',
                'headRepository': {'nameWithOwner': sync.REPO}, **updates}

    def test_approval_and_partial_merge_do_not_finish_task(self):
        self.assertEqual(sync.pr_task_status(self.issue(), [self.pr(reviewDecision='APPROVED')]), 'In review')
        self.assertEqual(sync.pr_task_status(self.issue(), [self.pr(state='MERGED')]), 'In progress')
        self.assertEqual(sync.pr_task_status(self.issue(), [self.pr(state='CLOSED')]), 'In progress')
        self.assertEqual(sync.pr_task_status(self.issue('- [x] everything'), [self.pr(state='MERGED')]), 'Done')

    def test_draft_or_changes_requested_wins_over_other_reviews(self):
        self.assertEqual(sync.pr_task_status(self.issue(), [self.pr(), self.pr(isDraft=True)]), 'In progress')
        self.assertEqual(sync.pr_task_status(self.issue(), [self.pr(), self.pr(reviewDecision='CHANGES_REQUESTED')]), 'In progress')
        self.assertIn('승인 · 병합 대기', sync.pr_summary(self.pr(reviewDecision='APPROVED')))
        self.assertIn('CI 미보고', sync.pr_summary(self.pr()))

    def test_dry_run_does_not_write_and_ignores_fork_or_mismatched_task(self):
        board = Mock(id='project')
        prs = [self.pr(), self.pr(headRefName='feature/T1-other'), self.pr(headRepository={'nameWithOwner': 'fork/platform'})]
        issues = {'T29': {**self.issue(), 'number': 33, 'id': 'issue'}}
        fields = {'node': {'fields': {'nodes': []}}}
        with patch.object(sync, 'load_issues', return_value=issues), patch.object(sync, 'Board', return_value=board), \
             patch.object(sync, 'load_prs', return_value=prs), patch.object(sync, 'gql', return_value=fields) as gql, \
             patch.object(sync, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            sync.sync_prs(True)
        board.set.assert_called_once_with(33, 'issue', 'In review', 'T29 #33', True)
        self.assertEqual(gql.call_count, 1)
        run.assert_not_called()

    def test_board_pages_include_later_issue_and_ignore_pr_cards(self):
        responses = [
            {'organization': {'projectV2': {'id': 'p', 'fields': {'nodes': [{'id': 'f', 'name': 'Status', 'options': []}]}}}},
            {'node': {'items': {'nodes': [{'id': 'pr', 'content': {}, 'fieldValueByName': None}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}}}},
            {'node': {'items': {'nodes': [{'id': 'i', 'content': {'number': 5, 'repository': {'nameWithOwner': sync.REPO}}, 'fieldValueByName': {'name': 'In review'}}], 'pageInfo': {'hasNextPage': False}}}},
        ]
        with patch.object(sync, 'gql', side_effect=responses) as gql:
            self.assertEqual(sync.Board().status(5), 'In review')
        self.assertEqual(gql.call_args.kwargs['after'], 'next')

    def test_legacy_pr51_reaches_existing_t5_card(self):
        board = Mock(id='project')
        pr = self.pr(number=51, title='[T5] 6개 통제 시험 설계', headRefName='t5-control-contract')
        issues = {'T5': {**self.issue(), 'number': 9, 'id': 'issue-9'}}
        with patch.object(sync, 'load_issues', return_value=issues), patch.object(sync, 'Board', return_value=board), \
             patch.object(sync, 'load_prs', return_value=[pr]), \
             patch.object(sync, 'gql', return_value={'node': {'fields': {'nodes': []}}}), \
             contextlib.redirect_stdout(io.StringIO()):
            sync.sync_prs(True)
        board.set.assert_called_once_with(9, 'issue-9', 'In review', 'T5 #9', True)

    def test_legacy_exception_does_not_accept_other_number_task_repo_or_base(self):
        pr = self.pr(number=51, title='[T5] controls', headRefName='t5-control-contract')
        self.assertEqual(sync.pr_task(pr), 'T5')
        for change in ({'number': 52}, {'title': '[T1] controls'}, {'headRefName': 't5-other'},
                       {'baseRefName': 'main'}, {'headRepository': None},
                       {'headRepository': {'nameWithOwner': 'fork/platform'}}):
            with self.subTest(change=change):
                self.assertIsNone(sync.pr_task({**pr, **change}))
        self.assertIsNone(sync.pr_task(self.pr(baseRefName='main')))
        self.assertIsNone(sync.pr_task(self.pr(title='[T29]workflow')))
