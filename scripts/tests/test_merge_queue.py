"""Native-queue registration: no approvals, stale mutation or retry of failed groups."""
import copy
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
bot = importlib.import_module("merge_queue")
HEAD = "a" * 40
OTHER = "b" * 40


def ready_pr():
    return {
        "id": "PR_fixture", "number": 42, "state": "OPEN", "isDraft": False,
        "isCrossRepository": False, "baseRefName": "develop", "headRefName": "feature/T1-example",
        "headRefOid": HEAD, "headRepository": {"nameWithOwner": bot.REPO},
        "mergeable": "MERGEABLE", "reviewDecision": None, "autoMergeRequest": None,
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "mergeQueueEntry": None,
        "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": False}, "nodes": [
                {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": "SUCCESS",
                 "checkSuite": {"app": {"slug": "github-actions"}}} for name in sorted(bot.REQUIRED)
            ]}}}}]},
        "timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": []},
    }


def removal(pr_head=HEAD):
    return {"beforeCommit": {"oid": "c" * 40, "parents": {
        "pageInfo": {"hasNextPage": False}, "nodes": [{"oid": OTHER}, {"oid": pr_head}]
    }}}


class Client:
    def __init__(self, initial=None, fresh=None):
        self.initial, self.fresh = initial or ready_pr(), fresh
        self.calls, self.reads, self.profiles = [], 0, 0
        self.receipt = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "queue-entry"}}}}

    def profile(self):
        self.profiles += 1
        return set(bot.REQUIRED)

    def read_pr(self, number):
        self.reads += 1
        return copy.deepcopy(self.fresh if self.reads > 1 and self.fresh is not None else self.initial)

    def graphql(self, query, **variables):
        self.calls.append((query, variables))
        return self.receipt

    def call(self, endpoint):
        self.calls.append(endpoint)
        return [{"number": 42, "head": {"sha": HEAD}}]


class RegistrationTest(unittest.TestCase):
    def test_held_pr_dequeues_with_the_provider_input_contract(self):
        class SchemaClient(Client):
            def graphql(self, query, **variables):
                if "dequeuePullRequest" in query:
                    # GitHub DequeuePullRequestInput requires `id`; enqueue uses
                    # a different input type. Model the provider rejecting an
                    # unknown/missing field instead of accepting any mutation.
                    fields = set(re.findall(r"(\w+)\s*:", query.split("input:{", 1)[1].split("}", 1)[0]))
                    if "id" not in fields or fields - {"id", "clientMutationId"}:
                        raise bot.QueueError("NATIVE_BLOCKED")
                return super().graphql(query, **variables)

        pr = ready_pr()
        pr.update(isDraft=True, mergeQueueEntry={"id": "entry"},
                  autoMergeRequest={"enabledAt": "fixture"})
        client = SchemaClient(pr)
        self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "HELD")
        self.assertEqual(client.calls[0][1], {"id": pr["id"]})
        self.assertEqual([query for query, _ in client.calls], [bot.DEQUEUE, bot.DISABLE_AUTO])

    def test_ready_pr_needs_no_human_approval_and_uses_server_head_cas(self):
        client = Client()
        client.initial["reviewRequests"] = ["all-team-members"]
        result = bot.reconcile(client, 42, apply=True)
        self.assertEqual(result["action"], "enqueue")
        self.assertEqual(client.calls, [(bot.ENQUEUE, {"id": "PR_fixture", "head": HEAD})])
        self.assertIn("expectedHeadOid:$head", client.calls[0][0])
        self.assertNotIn("mergePullRequest", client.calls[0][0])

    def test_dry_run_never_mutates(self):
        client = Client()
        self.assertEqual(bot.reconcile(client, 42)["result"], "READY")
        self.assertEqual(client.calls, [])

    def test_main_closed_fork_and_existing_sync_bot_stay_out_of_scope(self):
        for changes, expected in [({"baseRefName": "main"}, "OUT_OF_SCOPE"),
                                  ({"state": "CLOSED"}, "OUT_OF_SCOPE"),
                                  ({"isCrossRepository": True}, "OUT_OF_SCOPE"),
                                  ({"headRepository": None}, "OUT_OF_SCOPE"),
                                  ({"headRefName": "bot/sync-tasks"}, "SYNC_MANUAL")]:
            with self.subTest(changes=changes):
                pr = ready_pr()
                pr.update(changes)
                client = Client(pr)
                self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], expected)
                self.assertEqual(client.calls, [])

    def test_draft_change_request_conflict_and_hold_pause_but_never_approve(self):
        for changes in [{"isDraft": True}, {"reviewDecision": "CHANGES_REQUESTED"},
                        {"mergeable": "CONFLICTING"},
                        {"labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": "merge-queue:hold"}]}}]:
            with self.subTest(changes=changes):
                pr = ready_pr()
                pr.update(changes)
                pr.update(mergeQueueEntry={"id": "entry"}, autoMergeRequest={"enabledAt": "fixture"})
                client = Client(pr)
                self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "HELD")
                self.assertEqual([query for query, _ in client.calls], [bot.DEQUEUE, bot.DISABLE_AUTO])

    def test_no_op_when_already_queued_even_after_operator_retry(self):
        pr = ready_pr()
        pr["mergeQueueEntry"] = {"id": "entry"}
        pr["timelineItems"]["nodes"] = [removal()]
        client = Client(pr)
        self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "ALREADY_QUEUED")
        self.assertEqual(client.calls, [])

    def test_all_non_success_required_results_and_missing_context_block(self):
        for conclusion in [None, "FAILURE", "CANCELLED", "SKIPPED", "NEUTRAL", "TIMED_OUT"]:
            pr = ready_pr()
            pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]["conclusion"] = conclusion
            client = Client(pr)
            self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "WAITING_CI")
            self.assertEqual(client.calls, [])
        pr = ready_pr()
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"].pop()
        self.assertEqual(bot.reconcile(Client(pr), 42, apply=True)["result"], "WAITING_CI")

    def test_legacy_success_cannot_hide_actual_check_failure(self):
        pr = ready_pr()
        nodes = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
        nodes[0]["conclusion"] = "FAILURE"
        nodes.append({"__typename": "StatusContext", "context": nodes[0]["name"], "state": "SUCCESS"})
        self.assertFalse(bot.ready_checks(pr, bot.REQUIRED))

    def test_primary_contexts_require_the_actions_check_app(self):
        pr = ready_pr()
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]["checkSuite"]["app"]["slug"] = "other-app"
        self.assertFalse(bot.ready_checks(pr, bot.REQUIRED))

    def test_removed_merge_group_blocks_same_head_but_not_a_new_revision(self):
        pr = ready_pr()
        pr["timelineItems"]["nodes"] = [removal()]
        client = Client(pr)
        self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "MANUAL_RETRY_REQUIRED")
        self.assertEqual(client.calls, [])
        pr["timelineItems"]["nodes"] = [removal("d" * 40)]
        self.assertEqual(bot.reconcile(Client(pr), 42)["result"], "READY")

    def test_previous_group_parent_does_not_misidentify_this_pr(self):
        pr = ready_pr()
        event = removal(OTHER)
        event["beforeCommit"]["parents"]["nodes"][0]["oid"] = HEAD
        pr["timelineItems"]["nodes"] = [event]
        self.assertFalse(bot.removed_at_head(pr))

    def test_incomplete_or_unbound_metadata_is_not_an_enqueue_permission(self):
        for section, flag in [("labels", "hasNextPage"), ("timelineItems", "hasPreviousPage")]:
            pr = ready_pr()
            pr[section]["pageInfo"][flag] = True
            client = Client(pr)
            with self.assertRaises(bot.QueueError):
                bot.reconcile(client, 42, apply=True)
            self.assertEqual(client.calls, [])
        pr = ready_pr()
        pr["timelineItems"]["nodes"] = [{"beforeCommit": None}]
        with self.assertRaises(bot.QueueError):
            bot.reconcile(Client(pr), 42)

    def test_stale_event_or_changed_live_head_never_mutates(self):
        client = Client()
        self.assertEqual(bot.reconcile(client, 42, apply=True, expected_head=OTHER)["result"], "STALE_EVENT")
        fresh = ready_pr()
        fresh["headRefOid"] = OTHER
        client = Client(fresh=fresh)
        self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "STATE_CHANGED")
        self.assertEqual(client.calls, [])

    def test_new_hold_or_lost_profile_before_submission_never_mutates(self):
        fresh = ready_pr()
        fresh["reviewDecision"] = "CHANGES_REQUESTED"
        client = Client(fresh=fresh)
        self.assertEqual(bot.reconcile(client, 42, apply=True)["result"], "STATE_CHANGED")
        self.assertEqual(client.calls, [])
        client = Client()
        with (patch.object(client, "profile", side_effect=[set(bot.REQUIRED), bot.QueueError("QUEUE_PROFILE_UNAVAILABLE")]),
              self.assertRaises(bot.QueueError)):
            bot.reconcile(client, 42, apply=True)
        self.assertEqual(client.calls, [])

    def test_unconfirmed_mutation_does_not_retry(self):
        client = Client()
        client.receipt = {"data": {"enqueuePullRequest": None}}
        with self.assertRaisesRegex(bot.QueueError, "QUEUE_SUBMISSION_UNCONFIRMED"):
            bot.reconcile(client, 42, apply=True)
        self.assertEqual(len(client.calls), 1)


class EventAndBoundaryTest(unittest.TestCase):
    def test_effective_queue_profile_cannot_be_missing_or_weaken_required_ci(self):
        valid = [{"type": "merge_queue", "parameters": {"merge_method": "MERGE"}},
                 {"type": "required_status_checks", "parameters": {
                     "required_status_checks": [{"context": name} for name in sorted(bot.REQUIRED)]}}]
        client = bot.GitHub()
        with patch.object(client, "call", return_value=valid):
            self.assertEqual(client.profile(), bot.REQUIRED)
        for invalid in [valid[1:], valid[:1],
                        [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}, valid[1]],
                        [valid[0], {"type": "required_status_checks", "parameters": {
                            "required_status_checks": [{"context": "secrets"}, {"context": "platform"}]}}]]:
            with patch.object(client, "call", return_value=invalid), self.assertRaises(bot.QueueError):
                client.profile()

    def test_apply_requires_flag_and_trusted_repo_workflow_ref(self):
        valid = {"BRAVO_AUTO_QUEUE_ENABLED": "true", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": bot.REPO,
                 "GITHUB_REF": "refs/heads/develop", "GH_TOKEN": "synthetic-token",
                 "GITHUB_WORKFLOW_REF": f"{bot.REPO}/.github/workflows/merge-queue.yml@refs/heads/develop"}
        bot.require_apply_boundary(valid)
        for key in valid:
            changed = dict(valid)
            changed.pop(key)
            with self.subTest(key=key), self.assertRaises(bot.QueueError):
                bot.require_apply_boundary(changed)
        changed = dict(valid, GITHUB_REF="refs/pull/1/merge")
        with self.assertRaises(bot.QueueError):
            bot.require_apply_boundary(changed)

    def test_queue_group_callback_never_requeues(self):
        client = Client()
        self.assertEqual(bot.candidates(client, "workflow_run", {"workflow_run": {"name": "ci", "event": "merge_group"}}), [])
        self.assertEqual(client.calls, [])

    def test_ci_event_binds_head_and_review_signal_rechecks_live_state(self):
        run = {"name": "ci", "event": "pull_request", "head_sha": HEAD, "pull_requests": [{"number": 42}]}
        self.assertEqual(bot.candidates(Client(), "workflow_run", {"workflow_run": run}), [(42, HEAD)])
        run.update(name="pr-review-signal", event="pull_request_review")
        self.assertEqual(bot.candidates(Client(), "workflow_run", {"workflow_run": run}), [(42, None)])
        run["head_sha"] = None
        with self.assertRaises(bot.QueueError):
            bot.candidates(Client(), "workflow_run", {"workflow_run": run})

    def test_ci_with_missing_association_uses_commit_metadata_not_artifacts(self):
        client = Client()
        run = {"name": "ci", "event": "pull_request", "head_sha": HEAD, "pull_requests": []}
        self.assertEqual(bot.candidates(client, "workflow_run", {"workflow_run": run}), [(42, HEAD)])
        self.assertEqual(client.calls, [f"repos/{bot.REPO}/commits/{HEAD}/pulls?per_page=100"])

    def test_manual_or_schedule_sweep_recovers_coalesced_events(self):
        for event in ["schedule", "workflow_dispatch"]:
            client = Client()
            self.assertEqual(bot.candidates(client, event, {}), [(42, HEAD)])
            self.assertIn("state=open&base=develop", client.calls[0])

    def test_untrusted_numbers_and_revision_values_are_rejected(self):
        for value in [True, 0, -1, "42;echo bad", "$(echo bad)", "1\n2"]:
            with self.subTest(value=value), self.assertRaises(bot.QueueError):
                bot.number(value)
        with self.assertRaises(bot.QueueError):
            bot.candidates(Client(), "pull_request_target", {"pull_request": {"number": 42, "head": {"sha": None}}})

    def test_api_uses_argv_and_json_stdin_and_never_echoes_sdk_errors(self):
        result = subprocess.CompletedProcess([], 0, '{"data":{}}', "")
        with patch.object(bot.subprocess, "run", return_value=result) as run:
            bot.GitHub().graphql(bot.ENQUEUE, id='literal;$(echo bad)', head=HEAD)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["gh", "api", "graphql", "--input", "-"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(json.loads(kwargs["input"])["variables"]["id"], 'literal;$(echo bad)')
        with (patch.object(bot.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "sensitive fixture")),
              self.assertRaisesRegex(bot.QueueError, "^GITHUB_API_FAILED$")):
            bot.GitHub().call("fixture")


class WorkflowBoundaryTest(unittest.TestCase):
    def test_actual_shell_selects_dry_run_and_never_uses_a_missing_token(self):
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/merge-queue.yml").read_text()
        shell = "\n".join(line[10:] for line in workflow.split("        run: |\n", 1)[1].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "python3"
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BRAVO_TEST_ARGS"\n')
            executable.chmod(0o700)
            for dry in ["true", "false"]:
                args = root / (dry + ".txt")
                env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                       "GH_TOKEN": "synthetic-fixture", "DRY_RUN": dry, "BRAVO_TEST_ARGS": str(args)}
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell], env=env,
                                        capture_output=True, timeout=5, check=False)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(args.read_text().splitlines(), ["scripts/merge_queue.py"] + ([] if dry == "true" else ["--apply"]))
            env.update(GH_TOKEN="", BRAVO_TEST_ARGS=str(root / "never.txt"))
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell], env=env,
                                    capture_output=True, timeout=5, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "never.txt").exists())

    def test_privileged_workflow_has_no_pr_checkout_artifact_or_extra_secret(self):
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/merge-queue.yml").read_text()
        self.assertIn("ref: ${{ github.workflow_sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("github.event.pull_request.head", workflow)
        self.assertNotIn("download-artifact", workflow)
        self.assertNotIn("id-token:", workflow)
        self.assertEqual(workflow.count("secrets."), 1)
        self.assertIn("secrets.PROJECT_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
