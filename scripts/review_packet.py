"""Read-only, revision-bound GitHub review packets. Never approves or merges a PR."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

MAX_PAGES = 30
UTC = dt.timezone.utc  # noqa: UP017 - keep the standalone CLI usable on macOS Python 3.9.
SHA = re.compile(r"[0-9a-f]{40}")
SOURCE_DOCS = ("AGENTS.md", "docs/design.html", "docs/tasks.md", "docs/development.md")


class PacketError(RuntimeError):
    """A complete/current snapshot could not be established."""


class GitHub:
    def __init__(self, repo: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Expected owner/repository")
        self.repo = repo

    def call(self, *args: str) -> object:
        try:
            result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PacketError("GitHub request unavailable or timed out; no conclusion") from exc
        if result.returncode:
            # stderr may include private request data. Never persist it or auth headers.
            raise PacketError("GitHub request failed; no conclusion")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise PacketError("GitHub returned invalid JSON; no conclusion") from exc

    def api(self, path: str) -> object:
        return self.call("api", "--method", "GET", f"repos/{self.repo}/{path}")

    def pages(self, path: str, key: str | None = None) -> list[dict]:
        result, total = [], None
        for page in range(1, MAX_PAGES + 1):
            data = self.api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if key:
                if not isinstance(data, dict) or type(data.get("total_count")) is not int:
                    raise PacketError("Missing pagination total; no conclusion")
                if total is not None and total != data["total_count"]:
                    raise PacketError("Pagination changed during collection; retry")
                total, data = data["total_count"], data.get(key)
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise PacketError("Unexpected paginated response; no conclusion")
            result.extend(data)
            if len(data) < 100:
                if total is not None and total != len(result):
                    raise PacketError("Incomplete pagination; no conclusion")
                return result
        raise PacketError("Pagination limit reached; packet would be incomplete")

    def gql(self, query: str, **variables: object) -> dict:
        if not query.lstrip().startswith("query"):
            raise PacketError("Only read-only GraphQL queries are supported")
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            args.extend(["-F" if type(value) is int else "-f", f"{name}={value}"])
        data = self.call(*args)
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
            raise PacketError("GraphQL response contains errors or missing data")
        return data["data"]

    def metadata(self, number: int) -> dict:
        owner, name = self.repo.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            number title body url state isDraft headRefOid baseRefOid headRefName baseRefName
            reviewDecision mergeStateStatus changedFiles author{login}
            autoMergeRequest{enabledAt mergeMethod}
          }}}"""
        data = self.gql(query, owner=owner, name=name, number=number)
        pr = (data.get("repository") or {}).get("pullRequest")
        if not isinstance(pr, dict) or not all(SHA.fullmatch(pr.get(k, "")) for k in ("headRefOid", "baseRefOid")):
            raise PacketError("PR revision unavailable; no conclusion")
        if (any(not isinstance(pr.get(k), str) for k in
                ("title", "url", "state", "headRefName", "baseRefName", "mergeStateStatus"))
                or type(pr.get("changedFiles")) is not int
                or type(pr.get("isDraft")) is not bool or pr.get("number") != number
                or "reviewDecision" not in pr):
            raise PacketError("Incomplete PR metadata; no conclusion")
        return pr

    def threads(self, number: int) -> list[dict]:
        owner, name = self.repo.split("/")
        cursor, result, seen = None, [], set()
        for _ in range(MAX_PAGES):
            after = ",after:$after" if cursor else ""
            variable = ",$after:String!" if cursor else ""
            query = """query($owner:String!,$name:String!,$number:Int!VAR){
              repository(owner:$owner,name:$name){pullRequest(number:$number){
                reviewThreads(first:100AFTER){nodes{id isResolved isOutdated path line
                  comments(first:1){nodes{databaseId url}}}
                  pageInfo{hasNextPage endCursor}}
              }}}""".replace("VAR", variable).replace("AFTER", after)
            data = self.gql(query, owner=owner, name=name, number=number,
                            **({"after": cursor} if cursor else {}))
            try:
                page = data["repository"]["pullRequest"]["reviewThreads"]
                nodes, info = page["nodes"], page["pageInfo"]
                if not isinstance(nodes, list) or type(info["hasNextPage"]) is not bool:
                    raise KeyError("pagination")
                result.extend(nodes)
                if not info["hasNextPage"]:
                    return result
                cursor = info["endCursor"]
                if not cursor or cursor in seen:
                    raise KeyError("cursor")
                seen.add(cursor)
            except (KeyError, TypeError) as exc:
                raise PacketError("Incomplete review thread pagination") from exc
        raise PacketError("Review thread pagination limit reached")


def required_checks(rules: list[dict], checks: list[dict], statuses: list[dict], head: str) -> dict:
    """Report only exact-head, latest required contexts. Missing evidence stays unknown."""
    required = []
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            required.extend(rule.get("parameters", {}).get("required_status_checks", []))
    if not required:
        return {"state": "UNKNOWN", "reason": "No required contexts returned by rules API", "checks": []}
    results = []
    for requirement in required:
        context, app = requirement.get("context"), requirement.get("integration_id")
        if not isinstance(context, str) or not context:
            raise PacketError("Unsupported required check rule")
        # A check run and a legacy status with the same name are both shown/required.
        matched = [c for c in checks if c.get("name") == context and
                   (app in (None, -1) or (c.get("app") or {}).get("id") == app)]
        newest = {}
        for check in matched:
            identity = (check.get("app") or {}).get("id")
            if identity not in newest or check["id"] > newest[identity]["id"]:
                newest[identity] = check
        observations = [{"kind": "check_run", "state": c.get("conclusion") if c.get("status") == "completed" else c.get("status"),
                         "sha": c.get("head_sha"), "url": c.get("html_url"), "app_id": (c.get("app") or {}).get("id")}
                        for c in newest.values()]
        legacy = [s for s in statuses if s.get("context") == context]
        if legacy:
            latest = max(legacy, key=lambda s: s["id"])
            observations.append({"kind": "commit_status", "state": latest.get("state"),
                                 "sha": head, "url": latest.get("target_url"),
                                 "app_binding": "UNKNOWN" if app not in (None, -1) else "unrestricted",
                                 "binding": "exact commit statuses endpoint"})
        passed = bool(observations) and all(c["sha"] == head and c["state"] == "success"
                                           and c.get("app_binding") != "UNKNOWN" for c in observations)
        results.append({"context": context, "integration_id": app,
                        "state": "PASS" if passed else "NOT_VERIFIED", "observations": observations})
    return {"state": "PASS" if all(c["state"] == "PASS" for c in results) else "NOT_VERIFIED", "checks": results}


def capture(github: GitHub, number: int) -> dict:
    before = github.metadata(number)
    head, base = before["headRefOid"], before["baseRefOid"]
    files = github.pages(f"pulls/{number}/files")
    if len(files) != before["changedFiles"] or len({f["filename"] for f in files}) != len(files):
        raise PacketError("Changed-file list incomplete or changed during collection")
    reviews = github.pages(f"pulls/{number}/reviews")
    comments = github.pages(f"pulls/{number}/comments")
    discussion = github.pages(f"issues/{number}/comments")
    threads = github.threads(number)
    checks = github.pages(f"commits/{head}/check-runs?filter=latest", "check_runs")
    statuses = github.pages(f"commits/{head}/statuses")
    rules = github.api(f"rules/branches/{quote(before['baseRefName'], safe='')}")
    if not isinstance(rules, list) or any(not isinstance(r, dict) for r in rules):
        raise PacketError("Effective branch rules unavailable")
    after = github.metadata(number)
    if any(before[k] != after[k] for k in ("headRefOid", "baseRefOid", "baseRefName", "state", "isDraft", "reviewDecision")):
        raise PacketError("PR head/base/state changed during collection; regenerate packet")
    task_ids = sorted(set(re.findall(r"\bT[1-9][0-9]*\b", before["title"] + " " + (before.get("body") or "") + " " + before["headRefName"])), key=lambda t: int(t[1:]))
    root_threads = {c["databaseId"]: t for t in threads for c in t.get("comments", {}).get("nodes", [])}
    ledger = []
    for kind, records in (("review_comment", comments), ("discussion", discussion), ("review", reviews)):
        for record in records:
            thread = root_threads.get(record.get("in_reply_to_id", record["id"])) if kind == "review_comment" else None
            ledger.append({"kind": kind, "id": record["id"], "url": record.get("html_url"),
                           "author": (record.get("user") or {}).get("login"), "path": record.get("path"),
                           "line": record.get("line"), "commit_sha": record.get("commit_id"),
                           "thread_id": thread["id"] if thread else None,
                           "thread_resolved": thread["isResolved"] if thread else None,
                           "disposition": "UNASSESSED", "evidence": [], "assessed_head": None,
                           "note": "Read the original linked comment; thread resolution is not approval."})
    opinions = {}
    for review in sorted(reviews, key=lambda r: r["id"]):
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            opinions[(review.get("user") or {}).get("login")] = review
    approvals = [{"reviewer": user, "commit_sha": r.get("commit_id"), "url": r.get("html_url")}
                 for user, r in opinions.items() if user and user != (before.get("author") or {}).get("login")
                 and (r.get("user") or {}).get("type") == "User" and not user.endswith("[bot]")
                 and r["state"] == "APPROVED" and r.get("commit_id") == head]
    metadata = {k: after[k] for k in after if k != "body"}
    return {"schema_version": 1, "captured_at": dt.datetime.now(UTC).isoformat(),
            "repository": github.repo, "capture_status": "COMPLETE", "pr": metadata,
            "task_ids": task_ids, "task_id_basis": "title/body/branch mentions; verify against changed scope",
            "source_documents": [{"path": p, "base_url": f"https://github.com/{github.repo}/blob/{base}/{p}",
                                  "proposed_url": f"https://github.com/{github.repo}/blob/{head}/{p}"} for p in SOURCE_DOCS],
            "changed_files": [{k: f[k] for k in ("filename", "previous_filename", "status", "additions", "deletions") if k in f} for f in files],
            "rules": rules, "rules_source": "GitHub effective branch rules API (rulesets); GitHub remains merge authority",
            "required_ci": required_checks(rules, checks, statuses, head),
            "current_head_approvals": approvals,
            "reviews": [{"id": r["id"], "reviewer": (r.get("user") or {}).get("login"),
                         "state": r.get("state"), "commit_sha": r.get("commit_id"), "url": r.get("html_url")} for r in reviews],
            "unresolved_threads": [t for t in threads if not t["isResolved"]],
            "ledger": ledger, "merge_authorization": "NONE: factual snapshot, not an approval or merge gate",
            "limitations": ["API reads are sequential, not transactional; refresh before acting.",
                            "Rules API reports effective rulesets, not classic branch protection; GitHub is merge authority.",
                            "Only current-head approval records are counted; GitHub reviewDecision is reported separately.",
                            "Human approval records do not establish repository permission eligibility; GitHub enforces it.",
                            "No raw comments, PR body, logs, patches or credential values are retained.",
                            "Source links are review inputs; the tool does not claim those documents were reviewed."]}


def cell(value: object) -> str:
    return str(value if value is not None else "UNKNOWN").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown(packet: dict) -> str:
    pr = packet["pr"]
    lines = [f"# PR #{pr['number']} review packet", "", f"[{cell(pr['title'])}]({pr['url']})", "",
             f"Captured: {packet['captured_at']}", f"Head: `{pr['headRefOid']}`", f"Base: `{pr['baseRefOid']}` ({cell(pr['baseRefName'])})", "",
             "This is a factual snapshot, not approval or merge authorization. Re-read GitHub before acting.", "",
             "| Fact | Observed value |", "|---|---|",
             f"| GitHub reviewDecision | {cell(pr['reviewDecision'])} |", f"| GitHub mergeStateStatus | {cell(pr['mergeStateStatus'])} |",
             f"| Draft | {pr['isDraft']} |", f"| Auto-merge request | {cell(pr.get('autoMergeRequest'))} |",
             f"| Exact-head approvals | {len(packet['current_head_approvals'])} |",
             f"| Required CI | {packet['required_ci']['state']} |", f"| Unresolved threads | {len(packet['unresolved_threads'])} |",
             f"| Task mentions (verify scope) | {', '.join(packet['task_ids']) or 'UNKNOWN'} |", "",
             "## Source documents", ""]
    lines += [f"- {d['path']}: [base]({d['base_url']}) / [proposed]({d['proposed_url']})" for d in packet["source_documents"]]
    lines += ["", "## Changed files", "", "| Path | Change |", "|---|---|"]
    lines += [f"| {cell(f['filename'])} | {cell(f['status'])} |" for f in packet["changed_files"]]
    lines += ["", "## Required checks", "", "| Context | Evidence |", "|---|---|"]
    lines += [f"| {cell(c['context'])} | {c['state']}: {cell(', '.join(str(o['state']) for o in c['observations']) or 'missing')} |" for c in packet["required_ci"]["checks"]]
    lines += ["", "## Live rules and reviews", "", "Rulesets only; inspect GitHub for classic protection and final merge requirements.", ""]
    for rule in packet["rules"]:
        parameters = rule.get("parameters", {})
        detail = f"; required approvals: {parameters['required_approving_review_count']}" if "required_approving_review_count" in parameters else ""
        lines.append(f"- {cell(rule['type'])}{detail}; ruleset ID: {cell(rule.get('ruleset_id'))}")
    lines += ["", "| Reviewer | Review | Commit |", "|---|---|---|"]
    lines += [f"| {cell(r['reviewer'])} | [{cell(r['state'])}]({r['url']}) | `{cell(r['commit_sha'])}` |" for r in packet["reviews"]]
    lines += ["", "## Human judgement ledger", "", "Copy this table into assessment.md in this packet directory. Add exact-head evidence before resolving a thread.", "",
              "| Source | Author / path | Disposition | Evidence and assessed head |", "|---|---|---|---|"]
    lines += [f"| [{x['kind']} {x['id']}]({x['url']}) | {cell(x['author'])} / {cell(x['path'])} | UNASSESSED | — |" for x in packet["ledger"]]
    lines += ["", "## Review checklist", "", "- [ ] Baseline design/tasks and current issue ownership agree with the change.",
              "- [ ] Reuse module boundaries; avoid duplicate state, policy, clients and orchestration.",
              "- [ ] Guardrails, sandbox, allowlists and deterministic verdict/execution remain enforced.",
              "- [ ] Cloud IAM, component tags, budget, cleanup and secrets use match the affected scope.",
              "- [ ] CI selects affected consumers, always reports required gates, and fails closed on missing evidence.",
              "- [ ] Local and applicable cloud validation receipts identify the tested commit and environment.",
              "- [ ] Every finding has a disposition and evidence; thread resolution does not substitute for approval.",
              "- [ ] GitHub reports current-head human approval, required CI, and addressed change requests before merge.",
              "", "## Limits", ""]
    lines += [f"- {x}" for x in packet["limitations"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prs", nargs="+", type=int)
    parser.add_argument("--repo", default="Softbank-Hackathon-Finals/platform")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if any(number < 1 for number in args.prs):
        parser.error("PR numbers must be positive")
    try:
        github = GitHub(args.repo)
    except ValueError as exc:
        parser.error(str(exc))
    failed = False
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for number in dict.fromkeys(args.prs):
        stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            packet = capture(github, number)
        except (PacketError, KeyError, TypeError, ValueError) as exc:
            failed = True
            packet = {"schema_version": 1, "repository": args.repo, "pr_number": number,
                      "capture_status": "UNKNOWN", "error": str(exc) if isinstance(exc, PacketError) else "Unsupported GitHub response; no conclusion",
                      "merge_authorization": "NONE"}
        directory = args.output_dir / f"pr-{number}-{stamp}"
        directory.mkdir(mode=0o700)
        (directory / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (directory / "packet.md").write_text(markdown(packet) if packet["capture_status"] == "COMPLETE" else
                                             f"# PR #{number}: UNKNOWN\n\n{packet['error']}\n\nNo approval or CI conclusion. Regenerate after fixing access/history.\n", encoding="utf-8")
        print(f"PR #{number}: {packet['capture_status']} — {directory}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
