"""Review snapshots fail closed on incomplete, changed, or stale evidence."""

import contextlib
import copy
import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
review = importlib.import_module("review_packet")
HEAD, BASE, OLD = "a" * 40, "b" * 40, "c" * 40
RULES = [{"type": "required_status_checks", "parameters": {
    "required_status_checks": [{"context": "platform"}]}}]


def check(conclusion="success", sha=HEAD, number=1, app=10):
    return {"name": "platform", "id": number, "app": {"id": app}, "head_sha": sha,
            "status": "completed", "conclusion": conclusion, "html_url": "https://example/check"}


def opinion(number, state="APPROVED", sha=HEAD, login="reviewer", user_type="User"):
    return {"id": number, "state": state, "commit_id": sha, "body": "DO_NOT_RETAIN",
            "html_url": "https://example/review", "user": {"login": login, "type": user_type}}


class FixtureGitHub:
    repo = "owner/repo"

    def __init__(self):
        self.pr = {"number": 73, "title": "[T29] Review T1", "body": "DO_NOT_RETAIN",
                   "url": "https://example/pr", "state": "OPEN", "isDraft": False,
                   "headRefOid": HEAD, "baseRefOid": BASE, "headRefName": "feature/T29-review",
                   "baseRefName": "develop", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
                   "changedFiles": 1, "author": {"login": "author"}, "autoMergeRequest": None}
        self.after = copy.deepcopy(self.pr)
        self.metadata = Mock(side_effect=[self.pr, self.after])
        self.records = {"pulls/73/files": [{"filename": "README.md", "status": "modified", "patch": "DO_NOT_RETAIN"}],
                        "pulls/73/reviews": [opinion(1)],
                        "pulls/73/comments": [{"id": 2, "body": "DO_NOT_RETAIN", "path": "README.md",
                                               "html_url": "https://example/comment", "user": {"login": "reviewer"}}],
                        "issues/73/comments": [],
                        f"commits/{HEAD}/check-runs?filter=latest": [check()],
                        f"commits/{HEAD}/statuses": []}
        self.thread = {"id": "thread1", "isResolved": False, "isOutdated": False, "path": "README.md",
                       "line": 1, "comments": {"nodes": [{"databaseId": 2, "url": "https://example/comment"}]}}

    def pages(self, path, key=None):
        return copy.deepcopy(self.records[path])

    def threads(self, number):
        return [copy.deepcopy(self.thread)]

    def api(self, path):
        return copy.deepcopy(RULES)


class CheckEvidenceTest(unittest.TestCase):
    def test_non_success_and_stale_head_never_pass(self):
        for result in ("failure", "cancelled", "skipped", "neutral", "timed_out", None):
            with self.subTest(result=result):
                self.assertEqual(review.required_checks(RULES, [check(result)], [], HEAD)["state"], "NOT_VERIFIED")
        self.assertEqual(review.required_checks(RULES, [check(sha=OLD)], [], HEAD)["state"], "NOT_VERIFIED")
        self.assertEqual(review.required_checks(RULES, [], [], HEAD)["state"], "NOT_VERIFIED")
        self.assertEqual(review.required_checks([], [check()], [], HEAD)["state"], "UNKNOWN")

    def test_latest_per_app_and_legacy_status_both_matter(self):
        result = review.required_checks(RULES, [check("failure"), check(number=2)], [], HEAD)
        self.assertEqual(result["state"], "PASS")
        result = review.required_checks(RULES, [check(), check("failure", app=20)], [], HEAD)
        self.assertEqual(result["state"], "NOT_VERIFIED")
        legacy = [{"id": 5, "context": "platform", "state": "failure"}]
        self.assertEqual(review.required_checks(RULES, [check()], legacy, HEAD)["state"], "NOT_VERIFIED")

    def test_app_restricted_rule_requires_proven_app(self):
        rules = copy.deepcopy(RULES)
        rules[0]["parameters"]["required_status_checks"][0]["integration_id"] = 10
        self.assertEqual(review.required_checks(rules, [check(app=20)], [], HEAD)["state"], "NOT_VERIFIED")
        self.assertEqual(review.required_checks(rules, [check()], [], HEAD)["state"], "PASS")
        legacy = [{"id": 5, "context": "platform", "state": "success"}]
        self.assertEqual(review.required_checks(rules, [], legacy, HEAD)["state"], "NOT_VERIFIED")


class CaptureTest(unittest.TestCase):
    def test_changed_head_base_or_review_state_invalidates_snapshot(self):
        for field, changed in (("headRefOid", OLD), ("baseRefOid", OLD), ("state", "CLOSED"),
                               ("baseRefName", "main"), ("reviewDecision", "CHANGES_REQUESTED")):
            with self.subTest(field=field):
                gh = FixtureGitHub()
                gh.after[field] = changed
                with self.assertRaises(review.PacketError):
                    review.capture(gh, 73)

    def test_partial_or_duplicate_file_list_refused(self):
        for files in ([], [{"filename": "same"}, {"filename": "same"}]):
            gh = FixtureGitHub()
            gh.pr["changedFiles"] = 2
            gh.records["pulls/73/files"] = files
            with self.assertRaises(review.PacketError):
                review.capture(gh, 73)

    def test_current_head_human_approval_only(self):
        gh = FixtureGitHub()
        gh.records["pulls/73/reviews"] = [opinion(1, sha=OLD), opinion(2, login="author"),
                                        opinion(3, login="bot[bot]", user_type="Bot")]
        gh.thread["isResolved"] = True
        packet = review.capture(gh, 73)
        self.assertEqual(packet["pr"]["reviewDecision"], "APPROVED")
        self.assertEqual(packet["current_head_approvals"], [])
        self.assertEqual(packet["unresolved_threads"], [])
        self.assertEqual(packet["ledger"][0]["disposition"], "UNASSESSED")
        self.assertTrue(packet["ledger"][0]["thread_resolved"])
        self.assertTrue(packet["merge_authorization"].startswith("NONE"))

    def test_change_request_supersedes_approval_but_comment_does_not(self):
        for state, expected in (("CHANGES_REQUESTED", 0), ("DISMISSED", 0), ("COMMENTED", 1)):
            gh = FixtureGitHub()
            gh.records["pulls/73/reviews"] += [opinion(2, state=state)]
            self.assertEqual(len(review.capture(gh, 73)["current_head_approvals"]), expected)

    def test_comment_bodies_and_patches_not_persisted(self):
        packet = review.capture(FixtureGitHub(), 73)
        self.assertNotIn("DO_NOT_RETAIN", json.dumps(packet))
        self.assertNotIn("DO_NOT_RETAIN", review.markdown(packet))
        self.assertEqual(packet["task_ids"], ["T1", "T29"])
        self.assertIn(BASE, packet["source_documents"][0]["base_url"])
        self.assertIn(HEAD, packet["source_documents"][0]["proposed_url"])


class TransportTest(unittest.TestCase):
    def test_http_is_explicit_get_and_bounded(self):
        gh = review.GitHub("owner/repo")
        with patch.object(review.subprocess, "run", return_value=Mock(returncode=0, stdout="[]")) as run:
            gh.api("pulls/73/files")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gh", "api", "--method", "GET", "repos/owner/repo/pulls/73/files"])
        self.assertEqual(kwargs["timeout"], 45)

    def test_graphql_mutation_rejected_before_transport(self):
        gh = review.GitHub("owner/repo")
        with patch.object(gh, "call") as call, self.assertRaises(review.PacketError):
            gh.gql("mutation { resolveReviewThread(input: {}) { clientMutationId } }")
        call.assert_not_called()

    def test_failures_timeout_invalid_json_and_graphql_partial_data(self):
        gh = review.GitHub("owner/repo")
        for result in (Mock(returncode=1, stdout="SECRET"), Mock(returncode=0, stdout="not json")):
            with patch.object(review.subprocess, "run", return_value=result), self.assertRaises(review.PacketError) as ctx:
                gh.api("pulls/73")
            self.assertNotIn("SECRET", str(ctx.exception))
        with patch.object(review.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 45)), self.assertRaises(review.PacketError):
            gh.api("pulls/73")
        with patch.object(gh, "call", return_value={"data": {"repository": {}}, "errors": [{"message": "partial"}]}), self.assertRaises(review.PacketError):
            gh.gql("query { viewer { login } }")

    def test_paginated_total_mismatch_missing_total_and_limit(self):
        gh = review.GitHub("owner/repo")
        for response in ({"total_count": 2, "check_runs": [{}]}, {"check_runs": [{}]}):
            with patch.object(gh, "api", return_value=response), self.assertRaises(review.PacketError):
                gh.pages("checks", "check_runs")
        with patch.object(review, "MAX_PAGES", 2), patch.object(gh, "api", return_value=[{}] * 100), self.assertRaises(review.PacketError):
            gh.pages("reviews")

    def test_repeated_thread_cursor_fails(self):
        gh = review.GitHub("owner/repo")
        response = {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}}}
        with patch.object(gh, "gql", return_value=response), self.assertRaises(review.PacketError):
            gh.threads(73)

    def test_multiple_prs_preserve_failed_packet_and_exit_nonzero(self):
        packet = review.capture(FixtureGitHub(), 73)
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["review_packet", "73", "74", "--output-dir", tmp]
            with patch.object(sys, "argv", argv), patch.object(review, "capture", side_effect=[packet, review.PacketError("Incomplete pagination")]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(), 1)
            packets = sorted(Path(tmp).glob("*/packet.json"))
            self.assertEqual(len(packets), 2)
            self.assertEqual(json.loads(packets[0].read_text())["capture_status"], "COMPLETE")
            failed = json.loads(packets[1].read_text())
            self.assertEqual(failed["capture_status"], "UNKNOWN")
            self.assertNotIn("current_head_approvals", failed)


if __name__ == "__main__":
    unittest.main()
