"""Turn a saved, validated Proposal into the T7 publication it needs (PROPOSE, before rehearsal).

Steps, each idempotent by publication_request_id(intent):
1. candidate branch `bravo-candidate/<id>` = registered base + the Proposal's replacements
   (Git Data API), and one draft PR marked do-not-merge;
2. one T7 `publish-candidate-image` dispatch on main (run-name `candidate <id>`);
3. once that run succeeded, the PublicationSelection for prepare_publication.
The App can write only demo-chat branches/PRs and dispatch workflows; main stays behind
its PR rules. Nothing here approves, merges, rebuilds after approval or runs candidate code.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .candidate_publication import REPOSITORY, WORKFLOW, CandidateGit, PublicationSelection
from .store import EventError

BRANCH = "bravo-candidate/{}"
RUN_NAME = "candidate {}"
DISPATCH_GRACE = 300  # seconds a dispatch may take to appear in the runs list
MAX_DISPATCHES = 2  # per request id, across restarts; then an operator pins the selection
# Exactly what the writer may hold; a wider or narrower installation token is refused (github_app).
WRITER_PERMISSIONS = {"contents": "write", "pull_requests": "write", "actions": "write"}


class CandidateWriter:
    def __init__(self, app, repository, *, workflow_sha, workflow_id, state_file, clock=time.time,
                 remote=f"https://github.com/{REPOSITORY}.git"):
        if app.repository != REPOSITORY:
            raise ValueError("candidate_app_repository_mismatch")
        if app.permissions != WRITER_PERMISSIONS:
            raise ValueError("candidate_writer_permissions_mismatch")
        self.app, self.git, self.remote = app, CandidateGit(repository), remote
        self.workflow_sha, self.workflow_id, self.clock = workflow_sha, workflow_id, clock
        self.state_file = Path(state_file)  # dispatch times survive restarts (no second POST in the grace)
        if not self.state_file.is_absolute() or not self.state_file.parent.is_dir():
            raise ValueError("candidate_writer_state_mount_required")

    def _dispatches(self, request_id):
        if not self.state_file.exists():
            return []
        return json.loads(self.state_file.read_text()).get(request_id, [])

    def _record_dispatch(self, request_id):
        data = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        data.setdefault(request_id, []).append(self.clock())
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, self.state_file)  # written before the POST: a lost response never re-dispatches early

    def _json(self, method, path, body=None):
        return json.loads(self.app.call(method, f"repos/{REPOSITORY}/{path}", body) or b"null")

    def _fetch(self, branch):
        # Public repository: an unauthenticated fetch of immutable objects into the host clone.
        subprocess.run(["git", "-C", str(self.git.repository), "-c", "core.hooksPath=/dev/null", "fetch",
                        "--no-tags", "--quiet", self.remote, f"+refs/heads/{branch}:refs/bravo/{branch}"],
                       capture_output=True, timeout=60, check=True)

    def _branch_head(self, branch):
        try:
            return self._json("GET", f"git/ref/heads/{branch}")["object"]["sha"]
        except Exception as error:
            if getattr(error, "status", None) == 404:
                return None
            raise

    def _commit(self, value, result, request_id):
        base = value.binding.source_sha
        self._fetch("main")
        modes = {}
        for edit in result.proposal.edits:
            row = self.git._git("ls-tree", "-z", base, "--", edit.path).split(b"\0")[0]
            if not row:
                raise EventError("candidate_branch_conflict")  # the Proposal edits a file absent at base
            modes[edit.path] = row.split()[0].decode()
        tree = self._json("GET", f"git/commits/{base}")["tree"]["sha"]
        new_tree = self._json("POST", "git/trees", {"base_tree": tree, "tree": [
            {"path": e.path, "mode": modes[e.path], "type": "blob", "content": e.replacement}
            for e in result.proposal.edits]})["sha"]
        return self._json("POST", "git/commits", {"tree": new_tree, "parents": [base],
            "message": f"Bravo candidate {request_id[:12]} (do not merge)\n\nproposal {result.proposal_digest}"})["sha"]

    def ensure_pull_request(self, value, result, request_id):
        """Return (pull number, head sha); the head must be exactly base + the Proposal."""
        branch = BRANCH.format(request_id)
        head = self._branch_head(branch)
        if head is None:
            head = self._commit(value, result, request_id)
            self._json("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        self._fetch(branch)
        selection_like = type("Head", (), {"candidate_sha": head})
        try:
            diff = self.git.verify(value, result, selection_like)
        except ValueError:
            raise EventError("candidate_branch_conflict") from None
        owner = REPOSITORY.split("/")[0]
        pulls = self._json("GET", f"pulls?head={owner}:{branch}&state=all")
        if not pulls:
            pulls = [self._json("POST", "pulls", {"head": branch, "base": "main", "draft": True,
                "title": f"[Bravo] migration candidate {request_id[:12]} (do not merge)",
                "body": "Created by the Bravo workflow host from a validated Proposal for T7 publication "
                        "and T15 rehearsal. Do not merge; approval applies the rehearsed image digest."})]
        if len(pulls) != 1 or pulls[0]["state"] != "open" or pulls[0]["head"]["sha"] != head:
            raise EventError("candidate_pull_request_conflict")
        return pulls[0]["number"], head, diff

    def _runs(self, request_id):
        data = self._json("GET", f"actions/workflows/{self.workflow_id}/runs?event=workflow_dispatch"
                                 "&branch=main&per_page=100")
        return sorted((r for r in data["workflow_runs"] if r.get("display_title") == RUN_NAME.format(request_id)
                       and r.get("head_sha") == self.workflow_sha and r.get("path") == WORKFLOW),
                      key=lambda r: r["id"])

    def selection(self, value, result, request_id):
        """PublicationSelection once T7 succeeded; EventError while pending or after a failure."""
        pull, head, diff = self.ensure_pull_request(value, result, request_id)
        runs = self._runs(request_id)
        if not runs:
            sent = self._dispatches(request_id)
            if len(sent) >= MAX_DISPATCHES and self.clock() - sent[-1] > DISPATCH_GRACE:
                raise EventError("candidate_dispatch_limit")  # an operator pins the T7 run instead
            if not sent or self.clock() - sent[-1] > DISPATCH_GRACE:
                self._record_dispatch(request_id)
                self.app.call("POST", f"repos/{REPOSITORY}/actions/workflows/{self.workflow_id}/dispatches",
                              {"ref": "main", "inputs": {"candidate_pr": str(pull),
                               "base_sha": value.binding.source_sha, "source_sha": head,
                               "diff_sha256": diff, "request_id": request_id}})
            raise EventError("candidate_publication_pending")
        # ponytail: duplicates (a dispatch lost in the grace window) are equal evidence; take the first.
        run = runs[0]
        if run["status"] != "completed":
            raise EventError("candidate_publication_pending")
        if run["conclusion"] != "success" or run["run_attempt"] != 1:
            raise EventError("candidate_publication_failed")  # retries need an operator-pinned selection
        names = {a["name"]: a["id"] for a in self._json("GET", f"actions/runs/{run['id']}/artifacts")["artifacts"]}
        rid = run["id"]
        try:
            return PublicationSelection(workflow_sha=self.workflow_sha, workflow_id=self.workflow_id, run_id=rid,
                build_attempt=1, ghcr_attempt=1, ecr_attempt=1,
                build_artifact_id=names[f"candidate-bundle-{rid}-1"],
                ghcr_artifact_id=names[f"candidate-ghcr-receipt-{rid}-1"],
                ecr_artifact_id=names[f"candidate-ecr-receipt-{rid}-1"], candidate_sha=head, pull_request=pull)
        except (KeyError, ValueError):
            raise EventError("candidate_publication_failed") from None
