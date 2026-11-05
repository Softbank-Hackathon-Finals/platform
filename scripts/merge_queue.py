"""Register ready PRs in GitHub's queue; never merge, approve or rewrite branches."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

REPO = "Softbank-Hackathon-Finals/platform"
OWNER, NAME = REPO.split("/")
BASE = "develop"
REQUIRED = {"secrets", "infra", "platform"}
HOLD_LABELS = {"merge-queue:hold", "do-not-merge"}
SHA = re.compile(r"[0-9a-f]{40}")
QUERY = """query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){pullRequest(number:$number){
 id number state isDraft isCrossRepository baseRefName headRefName headRefOid
 headRepository{nameWithOwner} mergeable reviewDecision autoMergeRequest{enabledAt}
 labels(first:100){pageInfo{hasNextPage} nodes{name}}
 mergeQueueEntry{id}
 commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){
 pageInfo{hasNextPage} nodes{__typename
 ... on CheckRun{name status conclusion checkSuite{app{slug}}}
 ... on StatusContext{context state}}}}}}}
 timelineItems(last:50,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){
 pageInfo{hasPreviousPage} nodes{... on RemovedFromMergeQueueEvent{
 beforeCommit{oid parents(first:3){pageInfo{hasNextPage} nodes{oid}}}}}}
 }}}
"""
ENQUEUE = """mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{
 pullRequestId:$id,expectedHeadOid:$head}){mergeQueueEntry{id}}}"""
DEQUEUE = """mutation($id:ID!){dequeuePullRequest(input:{pullRequestId:$id}){clientMutationId}}"""
DISABLE_AUTO = """mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}"""


class QueueError(RuntimeError):
    """Only fixed, code-owned failure codes may reach public logs."""


class GitHub:
    def call(self, endpoint, payload=None):
        args = ["gh", "api", endpoint]
        if payload is not None:
            args += ["--input", "-"]
        try:
            result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise QueueError("GITHUB_UNAVAILABLE") from None
        if result.returncode:
            raise QueueError("GITHUB_API_FAILED")
        try:
            doc = json.loads(result.stdout)
        except ValueError:
            raise QueueError("GITHUB_RESPONSE_INVALID") from None
        if isinstance(doc, dict) and doc.get("errors"):
            raise QueueError("GITHUB_GRAPHQL_FAILED")
        return doc

    def graphql(self, query, **variables):
        return self.call("graphql", {"query": query, "variables": variables})

    def read_pr(self, number):
        doc = self.graphql(QUERY, owner=OWNER, name=NAME, number=number)
        try:
            pr = doc["data"]["repository"]["pullRequest"]
            if pr["number"] != number or not SHA.fullmatch(pr["headRefOid"]):
                raise ValueError
            return pr
        except (KeyError, TypeError, ValueError):
            raise QueueError("PR_RESPONSE_INVALID") from None

    def profile(self):
        rules = self.call(f"repos/{REPO}/rules/branches/{BASE}")
        contexts = set()
        queue = False
        for rule in rules:
            parameters = rule.get("parameters") or {}
            if rule["type"] == "merge_queue":
                queue = parameters.get("merge_method") == "MERGE"
            if rule["type"] == "required_status_checks":
                contexts.update(c["context"] for c in parameters["required_status_checks"])
        if not queue or not REQUIRED <= contexts:
            raise QueueError("QUEUE_PROFILE_UNAVAILABLE")
        return contexts


def removed_at_head(pr):
    history = pr["timelineItems"]
    if history["pageInfo"]["hasPreviousPage"]:
        raise QueueError("REMOVAL_HISTORY_INCOMPLETE")
    for event in history["nodes"]:
        commit = event.get("beforeCommit")
        if not commit:
            raise QueueError("REMOVAL_REVISION_UNKNOWN")
        if commit["oid"] == pr["headRefOid"]:
            return True
        parents = commit["parents"]
        if parents["pageInfo"]["hasNextPage"] or len(parents["nodes"]) != 2:
            raise QueueError("REMOVAL_REVISION_UNKNOWN")
        # GitHub MERGE groups have the preceding group/base first, this PR head second.
        if parents["nodes"][1]["oid"] == pr["headRefOid"]:
            return True
    return False


def ready_checks(pr, required):
    commits = pr["commits"]["nodes"]
    if len(commits) != 1 or commits[0]["commit"]["oid"] != pr["headRefOid"]:
        raise QueueError("CHECK_REVISION_UNKNOWN")
    rollup = commits[0]["commit"]["statusCheckRollup"]
    if rollup is None:
        return False
    contexts = rollup["contexts"]
    if contexts["pageInfo"]["hasNextPage"]:
        raise QueueError("CHECK_HISTORY_INCOMPLETE")
    for name in required:
        matches = [c for c in contexts["nodes"] if c.get("name", c.get("context")) == name]
        if not matches:
            return False
        if name in REQUIRED and not any(
            c["__typename"] == "CheckRun" and
            ((c.get("checkSuite") or {}).get("app") or {}).get("slug") == "github-actions"
            for c in matches
        ):
            return False
        for check in matches:
            if check["__typename"] == "CheckRun":
                if check.get("status") != "COMPLETED" or check.get("conclusion") != "SUCCESS":
                    return False
            elif check["__typename"] != "StatusContext" or check.get("state") != "SUCCESS":
                return False
    return True


def decide(pr, required):
    if (pr["state"] != "OPEN" or pr["baseRefName"] != BASE or pr["isCrossRepository"] or
            (pr.get("headRepository") or {}).get("nameWithOwner") != REPO):
        return "OUT_OF_SCOPE", "none"
    # Keep the existing bot/sync-tasks manual-registration policy.
    if pr["headRefName"].startswith("bot/"):
        return "SYNC_MANUAL", "none"
    labels = pr["labels"]
    if labels["pageInfo"]["hasNextPage"]:
        raise QueueError("LABELS_INCOMPLETE")
    held = HOLD_LABELS.intersection(n["name"] for n in labels["nodes"])
    if pr["isDraft"] or pr["reviewDecision"] == "CHANGES_REQUESTED" or held or pr["mergeable"] == "CONFLICTING":
        return "HELD", "pause"
    # An operator may explicitly retry a removed revision through GitHub's own queue UI.
    if pr["mergeQueueEntry"]:
        return "ALREADY_QUEUED", "none"
    if pr["mergeable"] != "MERGEABLE" or pr["reviewDecision"] == "REVIEW_REQUIRED":
        return "NATIVE_BLOCKED", "none"
    if removed_at_head(pr):
        return "MANUAL_RETRY_REQUIRED", "none"
    if not ready_checks(pr, required):
        return "WAITING_CI", "none"
    return "READY", "enqueue"


def reconcile(client, number, *, apply=False, expected_head=None):
    required = client.profile()
    pr = client.read_pr(number)
    result = {"number": number, "head": pr["headRefOid"], "dry_run": not apply}
    if expected_head is not None and pr["headRefOid"] != expected_head:
        return {**result, "result": "STALE_EVENT", "action": "none"}
    reason, action = decide(pr, required)
    if apply and action != "none":
        # Re-read authoritative state immediately before a mutation, not event-body claims.
        fresh = client.read_pr(number)
        new_required = client.profile()
        if fresh["headRefOid"] != pr["headRefOid"] or decide(fresh, new_required) != (reason, action):
            return {**result, "result": "STATE_CHANGED", "action": "none"}
        if action == "enqueue":
            # Queue-only API: no mergePullRequest, --admin, review dismissal or branch writes.
            receipt = client.graphql(ENQUEUE, id=fresh["id"], head=fresh["headRefOid"])
            try:
                entry = receipt["data"]["enqueuePullRequest"]["mergeQueueEntry"]["id"]
                if not isinstance(entry, str) or not entry:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise QueueError("QUEUE_SUBMISSION_UNCONFIRMED") from None
        else:
            if fresh["mergeQueueEntry"]:
                client.graphql(DEQUEUE, id=fresh["id"])
            if fresh["autoMergeRequest"]:
                client.graphql(DISABLE_AUTO, id=fresh["id"])
    return {**result, "result": reason, "action": action}


def number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(value)):
        raise QueueError("PR_NUMBER_INVALID")
    return int(value)


def candidates(client, event_name, event, selected=""):
    if selected:
        return [(number(selected), None)]
    if event_name == "pull_request_target":
        pr = event["pull_request"]
        if not isinstance(pr["head"]["sha"], str) or not SHA.fullmatch(pr["head"]["sha"]):
            raise QueueError("EVENT_REVISION_INVALID")
        return [(number(pr["number"]), pr["head"]["sha"])]
    if event_name == "workflow_run":
        run = event["workflow_run"]
        if run["name"] not in {"ci", "pr-review-signal"} or run["event"] not in {"pull_request", "pull_request_review"}:
            return []  # Native merge_group results never cause an automatic retry loop.
        if not isinstance(run.get("head_sha"), str) or not SHA.fullmatch(run["head_sha"]):
            raise QueueError("RUN_REVISION_INVALID")
        prs = run.get("pull_requests") or []
        if not prs:
            if not SHA.fullmatch(run["head_sha"]):
                raise QueueError("RUN_REVISION_INVALID")
            prs = client.call(f"repos/{REPO}/commits/{run['head_sha']}/pulls?per_page=100")
        if len(prs) >= 100:
            raise QueueError("CANDIDATES_INCOMPLETE")
        expected = run["head_sha"] if run["event"] == "pull_request" else None
        return sorted({(number(pr["number"]), expected) for pr in prs})
    if event_name not in {"workflow_dispatch", "schedule"}:
        raise QueueError("EVENT_UNSUPPORTED")
    prs = client.call(f"repos/{REPO}/pulls?state=open&base={BASE}&per_page=100")
    if len(prs) >= 100:
        raise QueueError("CANDIDATES_INCOMPLETE")
    return [(number(pr["number"]), pr["head"]["sha"]) for pr in prs]


def require_apply_boundary(env):
    if (env.get("BRAVO_AUTO_QUEUE_ENABLED") != "true" or env.get("GITHUB_ACTIONS") != "true" or
            env.get("GITHUB_REPOSITORY") != REPO or env.get("GITHUB_REF") != "refs/heads/develop" or
            env.get("GITHUB_WORKFLOW_REF") != f"{REPO}/.github/workflows/merge-queue.yml@refs/heads/develop" or
            not env.get("GH_TOKEN")):
        raise QueueError("APPLY_BOUNDARY_DENIED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pr", default="")
    args = parser.parse_args()
    if args.apply:
        require_apply_boundary(os.environ)
    client = GitHub()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()) if os.environ.get("GITHUB_EVENT_PATH") else {}
    inputs = event.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise QueueError("INPUTS_INVALID")
    selected = args.pr or str(inputs.get("pull_request") or "")
    results, failed = [], False
    deadline = time.monotonic() + 240
    for pr, head in candidates(client, event_name, event, selected):
        if time.monotonic() > deadline:
            raise QueueError("BATCH_DEADLINE")
        try:
            result = reconcile(client, pr, apply=args.apply, expected_head=head)
        except (QueueError, KeyError, TypeError, ValueError) as exc:
            code = str(exc) if isinstance(exc, QueueError) else "METADATA_INVALID"
            result, failed = {"number": pr, "result": code, "action": "none"}, True
        results.append(result)
        print(json.dumps(result, sort_keys=True))
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("### Merge Queue bot\n\n```json\n" + json.dumps(results, indent=2) + "\n```\n")
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QueueError, KeyError, TypeError, ValueError, OSError):
        print('{"result":"QUEUE_BOT_FAILED","action":"none"}')
        raise SystemExit(1) from None
