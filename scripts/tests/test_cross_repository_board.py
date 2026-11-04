"""Cross-repository PRs must not confuse task or PR numbers."""

import contextlib
import importlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sync = importlib.import_module("sync_tasks")


class CrossRepositoryBoardTest(unittest.TestCase):
    def setUp(self):
        self.issues = {"T22": {"id": "i15", "number": 15, "state": "OPEN", "body": "- [ ] live cutover"}}
        self.demo = f"{sync.OWNER}/demo-chat"

    def pr(self, **updates):
        return {
            "source_repository": self.demo, "number": 9, "title": "[T22] mobile chat",
            "headRefName": "feature/T22-mobile-reconnect", "baseRefName": "main",
            "headRepository": {"nameWithOwner": self.demo},
            "body": f"Description\n\nRefs {sync.REPO}#15\n", "state": "OPEN",
            "reviewDecision": "REVIEW_REQUIRED", "isDraft": False,
            "updatedAt": "2026-11-03T10:00:00Z", **updates,
        }

    def test_demo_requires_real_platform_task_reference_and_same_repo_head(self):
        self.assertEqual(sync.pr_task(self.pr(), self.issues), "T22")
        for changes in (
            {"body": "Refs #15"}, {"body": f"Refs {sync.REPO}#16"},
            {"body": "Refs attacker/platform#15"},
            {"body": f"Refs {sync.REPO}#150"}, {"body": ""},
            {"headRepository": {"nameWithOwner": "fork/demo-chat"}},
            {"headRepository": None}, {"source_repository": "attacker/demo-chat"},
            {"baseRefName": "develop"}, {"headRefName": "feature/T4-chat"},
            {"title": "[T29] workflow", "headRefName": "feature/T29-board"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(sync.pr_task(self.pr(**changes), self.issues))
        self.assertIsNone(sync.pr_task(self.pr()))

    def test_repository_numbers_are_displayed_and_latest_is_by_time(self):
        platform = self.pr(source_repository=sync.REPO, number=100,
                           updatedAt="2026-11-03T09:00:00Z")
        demo = self.pr(number=9)
        self.assertIn("platform#100", sync.pr_summary(platform))
        self.assertIn("demo-chat#9", sync.pr_summary(demo))
        self.assertIs(max([platform, demo], key=sync.pr_order), demo)

    def test_demo_review_merge_and_changes_request_preserve_completion_boundary(self):
        board = Mock(id="p", pr_states={15: ""}, items={15: ("item", "In progress")})
        fields = {"node": {"fields": {"nodes": [{"name": "PR 상태", "id": "f", "dataType": "TEXT"}]}}}
        for updates, state in (({}, "In review"), ({"state": "MERGED"}, "In progress"),
                               ({"reviewDecision": "CHANGES_REQUESTED"}, "In progress")):
            board.reset_mock()
            with patch.object(sync, "load_issues", return_value=self.issues), \
                 patch.object(sync, "Board", return_value=board), \
                 patch.object(sync, "load_prs", return_value=[self.pr(**updates)]), \
                 patch.object(sync, "gql", return_value=fields) as gql, \
                 contextlib.redirect_stdout(io.StringIO()):
                sync.sync_prs(True)
            board.set.assert_called_once_with(15, "i15", state, "T22 #15", True)
            gql.assert_not_called()  # Board is mocked; no field writes escape it.

    def page(self, number, more=False, cursor=None):
        return {"repository": {"pullRequests": {"nodes": [{"number": number}],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}

    def test_both_repositories_and_all_pages_are_collected_with_trusted_provenance(self):
        with patch.object(sync, "gql", side_effect=[self.page(100, True, "next"),
                          self.page(99), self.page(9)]) as gql:
            prs = sync.load_prs()
        self.assertEqual([p["source_repository"] for p in prs], [sync.REPO, sync.REPO, self.demo])
        self.assertEqual([c.kwargs["r"] for c in gql.call_args_list], ["platform", "platform", "demo-chat"])
        self.assertEqual(gql.call_args_list[1].kwargs["after"], "next")
        self.assertNotIn("after", gql.call_args_list[2].kwargs)

    def test_incomplete_cross_repo_read_never_mutates_cards(self):
        board = Mock()
        with patch.object(sync, "load_issues", return_value=self.issues), \
             patch.object(sync, "Board", return_value=board), \
             patch.object(sync, "gql", side_effect=[self.page(100), RuntimeError("unavailable")]), \
             self.assertRaisesRegex(RuntimeError, "unavailable"):
            sync.sync_prs(False)
        board.set.assert_not_called()

    def test_pagination_loop_and_missing_cursor_are_rejected(self):
        for responses in ([self.page(1, True)],
                          [self.page(1, True, "same"), self.page(2, True, "same")]):
            with self.subTest(responses=responses), patch.object(sync, "gql", side_effect=responses), \
                 self.assertRaisesRegex(RuntimeError, "pagination incomplete"):
                sync.load_prs()

    def test_pagination_limit_is_not_a_partial_success(self):
        with patch.object(sync, "MAX_PR_PAGES", 1), \
             patch.object(sync, "gql", return_value=self.page(1, True, "next")), \
             self.assertRaisesRegex(RuntimeError, "pagination limit"):
            sync.load_prs()


if __name__ == "__main__":
    unittest.main()
