"""Exercise stored card summaries at the GitHub API boundary."""

import contextlib
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sync = importlib.import_module("sync_tasks")


class BoardProgressTest(unittest.TestCase):
    def setUp(self):
        self.fields = {name: {"id": name, "name": name, "dataType": "TEXT"}
                       for name in sync.TEXT_FIELDS}
        self.values = {name: {} for name in sync.TEXT_FIELDS}
        self.states = {5: "In progress", 15: "Backlog"}
        self.issues = {
            "T1": {"number": 5, "id": "i5", "state": "OPEN", "assignees": [{"login": "owner"}],
                   "body": "- [x] implementation\n- [ ] integration"},
            "T22": {"number": 15, "id": "i15", "state": "OPEN", "assignees": [],
                    "body": "- [ ] cutover"},
        }
        self.writes = []
        self.patches = [patch.object(sync, "gql", side_effect=self.api),
                        patch.object(sync, "load_issues", return_value=self.issues),
                        patch.object(sync, "run")]
        self.api_mock, _, self.run = [p.start() for p in self.patches]
        self.addCleanup(patch.stopall)

    def api(self, query, **variables):
        if query.startswith("mutation"):
            self.writes.append((query, variables))
            if "createProjectV2Field" in query:
                name = variables["name"]
                self.fields[name] = {"id": name, "name": name, "dataType": "TEXT"}
                return {"createProjectV2Field": {"projectV2Field": {"id": name}}}
            number = int(variables["i"].removeprefix("item"))
            if "t" in variables:
                self.values[variables["f"]][number] = variables["t"]
            else:
                self.states[number] = variables["o"]
            return {}
        if "organization(" in query:
            status = {"name": "Status", "id": "status", "dataType": "SINGLE_SELECT",
                      "options": [{"id": name, "name": name} for name in
                                  ("Backlog", "In progress", "In review", "Done")]}
            return {"organization": {"projectV2": {"id": "project", "fields": {
                "nodes": [status, *self.fields.values()], "pageInfo": {"hasNextPage": False}}}}}
        nodes = []
        for number, state in self.states.items():
            nodes.append({"id": f"item{number}",
                          "content": {"number": number, "repository": {"nameWithOwner": sync.REPO}},
                          "fieldValueByName": {"name": state},
                          **{alias: {"text": self.values[name].get(number, "")} for name, alias in
                             zip(sync.TEXT_FIELDS, ("prStatus", "progress", "contributions"))}})
        return {"node": {"items": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}}

    def pr(self, **changes):
        return {"source_repository": sync.REPO, "number": 78, "title": "[T1] implementation",
                "headRefName": "feature/T1-implementation", "baseRefName": "develop",
                "headRepository": {"nameWithOwner": sync.REPO}, "body": "Refs #5",
                "state": "MERGED", "isDraft": False, "reviewDecision": "APPROVED",
                "author": {"login": "contributor"}, "updatedAt": "2026-11-03T10:00:00Z", **changes}

    def sync_prs(self, prs=(), dry=False):
        with patch.object(sync, "load_prs", return_value=list(prs)), contextlib.redirect_stdout(io.StringIO()):
            sync.sync_prs(dry)

    def test_partial_merge_shows_progress_and_author_without_changing_owner_or_completion(self):
        original = json.dumps(self.issues)
        self.sync_prs([self.pr()])
        self.assertEqual(self.values["할 일 진행"][5], "1/2 완료 · 1개 남음")
        self.assertEqual(self.values["병합 기여"][5], "@contributor 1 PR")
        self.assertEqual(self.states[5], "In progress")
        self.assertEqual(json.dumps(self.issues), original)
        self.run.assert_not_called()

    def test_no_pr_card_is_explicit_without_being_started(self):
        self.sync_prs()
        self.assertEqual(self.values["할 일 진행"][15], "0/1 완료 · 1개 남음")
        self.assertEqual(self.values["병합 기여"][15], "병합 PR 없음")
        self.assertEqual(self.states[15], "Backlog")

    def test_repeated_readback_is_idempotent_and_uncheck_updates_stored_progress(self):
        self.sync_prs([self.pr()])
        self.writes.clear()
        self.sync_prs([self.pr()])
        self.assertEqual(self.writes, [])
        self.issues["T1"]["body"] = "- [ ] implementation\n- [ ] integration"
        with patch.object(sync, "TASKS") as tasks, patch.object(sync, "rewrite_tasks", return_value="plan"), \
             patch.object(sync, "parse", return_value={"T1": sync.Task("T1", "test", "1")}), \
             contextlib.redirect_stdout(io.StringIO()):
            tasks.read_text.return_value = "plan"
            sync.pull(False)
        self.assertEqual(self.values["할 일 진행"][5], "0/2 완료 · 2개 남음")
        self.assertEqual(len(self.writes), 1)
        self.run.assert_not_called()

    def test_closed_issue_and_missing_checklist_are_not_reported_as_fully_checked(self):
        self.issues["T1"]["state"] = "CLOSED"
        self.issues["T22"]["body"] = "No checklist yet"
        self.sync_prs([self.pr()])
        self.assertEqual(self.states[5], "Done")
        self.assertEqual(self.values["할 일 진행"][5], "1/2 완료 · 1개 남음")
        self.assertEqual(self.values["할 일 진행"][15], "할 일 없음 · 완료 여부 확인 필요")

    def test_contributions_count_only_eligible_merged_prs_across_repositories(self):
        demo = f"{sync.OWNER}/demo-chat"
        common = {"title": "[T22] chat", "headRefName": "feature/T22-chat", "body": f"Refs {sync.REPO}#15"}
        platform = self.pr(**common)
        demo_pr = self.pr(**common, source_repository=demo, baseRefName="main", headRepository={"nameWithOwner": demo})
        self.sync_prs([platform, demo_pr, platform,  # same number in two repos; one duplicate
                      self.pr(**common, number=79, state="OPEN"),
                      self.pr(**common, number=80, state="CLOSED"),
                      self.pr(**common, number=81, headRepository={"nameWithOwner": "fork/platform"}),
                      {**demo_pr, "number": 82, "body": "Refs #15"},
                      self.pr(**common, number=83, author=None)])
        self.assertEqual(self.values["병합 기여"][15], "@contributor 2 PR · 작성자 미확인 1 PR")

    def test_dry_run_does_not_create_fields_or_write_cards(self):
        self.fields.clear()
        self.sync_prs([self.pr()], dry=True)
        self.assertEqual(self.fields, {})
        self.assertEqual(self.writes, [])
        self.run.assert_not_called()

    def test_missing_fields_are_created_once_and_read_back(self):
        self.fields.clear()
        self.sync_prs([self.pr()])
        creates = [variables["name"] for query, variables in self.writes if "createProjectV2Field" in query]
        self.assertEqual(creates, list(sync.TEXT_FIELDS))
        board = sync.Board()
        self.assertEqual(board.text_values["할 일 진행"][5], "1/2 완료 · 1개 남음")
        self.assertEqual(board.text_values["병합 기여"][5], "@contributor 1 PR")

    def test_field_type_conflict_stops_before_creating_other_fields_or_moving_cards(self):
        self.fields = {"병합 기여": {"id": "bad", "name": "병합 기여", "dataType": "SINGLE_SELECT"}}
        with self.assertRaisesRegex(RuntimeError, "병합 기여 must be a text field"):
            self.sync_prs([self.pr(state="OPEN")])
        self.assertEqual(self.writes, [])

    def test_failed_repository_read_preserves_existing_values(self):
        self.values["병합 기여"][5] = "@previous 2 PR"
        with patch.object(sync, "load_prs", side_effect=RuntimeError("demo-chat unavailable")), \
             self.assertRaisesRegex(RuntimeError, "unavailable"):
            sync.sync_prs(False)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.values["병합 기여"][5], "@previous 2 PR")

    def test_failed_field_write_is_not_cached_as_success(self):
        board = sync.Board()
        with patch.object(sync, "gql", side_effect=RuntimeError("write denied")), \
             self.assertRaisesRegex(RuntimeError, "denied"):
            board.set_text(5, "할 일 진행", "1/2 완료 · 1개 남음", "T1", False)
        self.assertEqual(board.text_values["할 일 진행"][5], "")

    def test_status_returns_stored_values_without_creating_fields_or_changing_issue(self):
        self.values["할 일 진행"][5] = "0/2 완료 · 2개 남음"  # Deliberately stale.
        self.values["병합 기여"][5] = "@contributor 1 PR"
        task = sync.Task("T1", "test", "1")
        with patch.object(sync, "TASKS"), patch.object(sync, "parse", return_value={"T1": task}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            sync.status()
        row = json.loads(output.getvalue())["tasks"][0]
        self.assertEqual((row["checked"], row["total"]), (1, 2))
        self.assertEqual(row["progress"], "0/2 완료 · 2개 남음")
        self.assertEqual(row["merged_contributions"], "@contributor 1 PR")
        self.assertEqual(self.writes, [])
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
