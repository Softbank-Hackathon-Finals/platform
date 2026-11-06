"""Proposal -> candidate PR -> one T7 dispatch -> PublicationSelection, against real git objects."""

import json
import os
import subprocess

import pytest
from test_candidate_publication import Bundle, git, repository  # noqa: F401  (fixture)

from bravo_platform_events import candidate_publication as candidate
from bravo_platform_events.candidate_writer import RUN_NAME, WRITER_PERMISSIONS, CandidateWriter
from bravo_platform_events.github_app import GitHubError
from bravo_platform_events.store import EventError

WORKFLOW_SHA, WORKFLOW_ID = "c" * 40, 7
P = f"repos/{candidate.REPOSITORY}/"


class FakeApp:
    """GitHub stand-in whose Git Data API writes real objects into the `remote` repository."""

    repository = candidate.REPOSITORY
    permissions = dict(WRITER_PERMISSIONS)

    def __init__(self, remote):
        self.remote, self.pulls, self.runs, self.dispatches = remote, [], [], []

    def _git(self, *args, env=None, data=None):
        return subprocess.run(["git", "-C", str(self.remote), *args], input=data, capture_output=True,
                              check=True, env={**os.environ, **(env or {})}).stdout.decode().strip()

    def call(self, method, path, body=None, *, limit=None):
        assert path.startswith(P)
        path = path[len(P):]
        if method == "GET" and path.startswith("git/ref/heads/"):
            try:
                return json.dumps({"object": {"sha": self._git("rev-parse", "refs/heads/" + path[14:])}}).encode()
            except subprocess.CalledProcessError:
                raise GitHubError(404, path) from None
        if method == "GET" and path.startswith("git/commits/"):
            return json.dumps({"tree": {"sha": self._git("rev-parse", path[12:] + "^{tree}")}}).encode()
        if (method, path) == ("POST", "git/trees"):
            env = {"GIT_INDEX_FILE": str(self.remote / ".git" / "fake-index")}
            self._git("read-tree", body["base_tree"], env=env)
            for entry in body["tree"]:
                oid = self._git("hash-object", "-w", "--stdin", data=entry["content"].encode())
                self._git("update-index", "--add", "--cacheinfo", f"{entry['mode']},{oid},{entry['path']}", env=env)
            return json.dumps({"sha": self._git("write-tree", env=env)}).encode()
        if (method, path) == ("POST", "git/commits"):
            sha = self._git("commit-tree", body["tree"], "-p", body["parents"][0], "-m", body["message"])
            return json.dumps({"sha": sha}).encode()
        if (method, path) == ("POST", "git/refs"):
            self._git("update-ref", body["ref"], body["sha"])
            return b"{}"
        if method == "GET" and path.startswith("pulls?"):
            return json.dumps(self.pulls).encode()
        if (method, path) == ("POST", "pulls"):
            head = self._git("rev-parse", "refs/heads/" + body["head"])
            self.pulls.append({"number": 41, "state": "open", "head": {"sha": head}})
            return json.dumps(self.pulls[-1]).encode()
        if method == "GET" and path.startswith(f"actions/workflows/{WORKFLOW_ID}/runs"):
            return json.dumps({"workflow_runs": self.runs}).encode()
        if (method, path) == ("POST", f"actions/workflows/{WORKFLOW_ID}/dispatches"):
            self.dispatches.append(body)
            return b""
        if method == "GET" and path.startswith("actions/runs/"):
            rid = int(path.split("/")[2])
            return json.dumps({"artifacts": [{"name": f"candidate-{n}-{rid}-1", "id": i} for i, n in
                               enumerate(("bundle", "ghcr-receipt", "ecr-receipt"), 301)]}).encode()
        raise AssertionError((method, path))


@pytest.fixture
def setup(repository, tmp_path):  # noqa: F811
    remote, base, _head = repository
    git(remote, "branch", "-M", "main")
    git(remote, "reset", "-q", "--hard", base)  # the fixture's own candidate commit is not ours
    clone = tmp_path / "host-clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    b = Bundle(repository)
    app = FakeApp(remote)
    clock = [1000.0]
    state = tmp_path / "writer-state" / "dispatches.json"
    state.parent.mkdir()

    def make():
        return CandidateWriter(app, clone, workflow_sha=WORKFLOW_SHA, workflow_id=WORKFLOW_ID, state_file=state,
                               clock=lambda: clock[0], remote=str(remote))
    writer = make()
    writer.make = make
    return b, app, writer, clock


def run(rid, request_id, **extra):
    return {"id": rid, "display_title": RUN_NAME.format(request_id), "head_sha": WORKFLOW_SHA,
            "path": candidate.WORKFLOW, "status": "completed", "conclusion": "success", "run_attempt": 1} | extra


def test_creates_one_pr_dispatches_once_then_selects_the_reviewed_run(setup):
    b, app, writer, clock = setup
    rid = "9" * 64
    with pytest.raises(EventError, match="candidate_publication_pending"):
        writer.selection(b.value, b.result, rid)
    with pytest.raises(EventError, match="candidate_publication_pending"):
        writer.selection(b.value, b.result, rid)  # grace window: no second dispatch
    assert len(app.pulls) == 1 and len(app.dispatches) == 1
    inputs = app.dispatches[0]["inputs"]
    head = app.pulls[0]["head"]["sha"]
    assert inputs["candidate_pr"] == "41" and inputs["source_sha"] == head
    assert inputs["base_sha"] == b.value.binding.source_sha and inputs["request_id"] == rid
    # The candidate is exactly base + the Proposal, so T10's own verifier accepts it.
    selected = type("S", (), {"candidate_sha": head})
    assert writer.git.verify(b.value, b.result, selected) == inputs["diff_sha256"]
    app.runs = [run(77, "other"), run(80, rid, head_sha="d" * 40), run(90, rid)]
    s = writer.selection(b.value, b.result, rid)
    assert (s.run_id, s.candidate_sha, s.pull_request, s.workflow_sha) == (90, head, 41, WORKFLOW_SHA)
    assert (s.build_artifact_id, s.ghcr_artifact_id, s.ecr_artifact_id) == (301, 302, 303)
    assert len(app.pulls) == 1 and len(app.dispatches) == 1


def test_lost_dispatch_is_retried_once_after_the_grace_window_even_across_restarts(setup):
    b, app, writer, clock = setup
    rid = "8" * 64
    with pytest.raises(EventError, match="pending"):
        writer.selection(b.value, b.result, rid)
    with pytest.raises(EventError, match="pending"):
        writer.make().selection(b.value, b.result, rid)  # restarted host inside the grace window
    assert len(app.dispatches) == 1
    clock[0] += 301
    with pytest.raises(EventError, match="pending"):
        writer.make().selection(b.value, b.result, rid)
    assert len(app.dispatches) == 2
    clock[0] += 301
    with pytest.raises(EventError, match="candidate_dispatch_limit"):
        writer.make().selection(b.value, b.result, rid)
    assert len(app.dispatches) == 2


def test_writer_needs_exactly_its_permissions_and_a_state_mount(setup, tmp_path):
    b, app, writer, clock = setup
    app.permissions = WRITER_PERMISSIONS | {"workflows": "write"}
    with pytest.raises(ValueError, match="permissions"):
        writer.make()
    app.permissions = dict(WRITER_PERMISSIONS)
    with pytest.raises(ValueError, match="state_mount"):
        CandidateWriter(app, writer.git.repository, workflow_sha=WORKFLOW_SHA, workflow_id=WORKFLOW_ID,
                        state_file=tmp_path / "missing" / "d.json")


def test_proposal_for_a_file_absent_at_base_is_a_conflict(setup):
    b, app, writer, clock = setup
    edit = b.result.proposal.edits[0].model_copy(update={"path": "app/absent.py"})
    proposal = b.result.proposal.model_copy(update={"edits": [edit]})
    with pytest.raises(EventError, match="candidate_branch_conflict"):
        writer.selection(b.value, b.result.model_copy(update={"proposal": proposal}), "4" * 64)
    assert app.dispatches == []


@pytest.mark.parametrize("change", [{"status": "in_progress"}, {"conclusion": "failure"}, {"run_attempt": 2}])
def test_unfinished_failed_or_retried_runs_do_not_select(setup, change):
    b, app, writer, _ = setup
    rid = "7" * 64
    app.runs = [run(90, rid, **change)]
    with pytest.raises(EventError, match="pending" if "status" in change else "failed"):
        writer.selection(b.value, b.result, rid)


def test_tampered_branch_or_closed_pr_is_a_conflict(setup):
    b, app, writer, _ = setup
    rid = "6" * 64
    git(app.remote, "branch", f"bravo-candidate/{rid}", "main")  # branch exists without the Proposal
    with pytest.raises(EventError, match="candidate_branch_conflict"):
        writer.selection(b.value, b.result, rid)
    rid = "5" * 64
    app.runs = [run(90, rid)]
    writer.selection(b.value, b.result, rid)
    app.pulls[0]["state"] = "closed"
    with pytest.raises(EventError, match="candidate_pull_request_conflict"):
        writer.selection(b.value, b.result, rid)
