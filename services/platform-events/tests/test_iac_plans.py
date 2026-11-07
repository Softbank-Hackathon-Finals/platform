"""T44 destination IaC through the platform API: plan, T10 infra approval, desired-state PR (iac_plans)."""

import base64
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bravo_iac.chart import files as chart_files
from bravo_iac.node import files as node_files
from bravo_iac.tailnet import fragment
from fastapi.testclient import TestClient
from test_cutover import NOW, PRINCIPAL, SPEC
from test_infra_approval import infra
from test_workflow_recovery import collect, service

from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.github_app import GitHubError
from bravo_platform_events.iac_plans import (
    WRITER_PERMISSIONS,
    GitHubIacRepository,
    IacPlanService,
    generated_files,
)
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.store import EventError

PLAN = SPEC.plan_id
LATER = (datetime.fromisoformat(NOW) + timedelta(hours=1)).isoformat()
REPO = Path(__file__).resolve().parents[3]
COMMITTED = {p.stem: json.loads(p.read_text()) for p in sorted((REPO / "infra/iac/environments").glob("*.json"))}
CLOSED = {"state": "closed", "bucket": "demo-chat-attachments"}
APPROVER = Principal("approver", PRINCIPAL.tenant_id)


def allow(*_):
    return None


class Repository:
    """The platform repository port over an in-memory desired state."""

    def __init__(self):
        self.data, self.sha, self.opened = copy.deepcopy(COMMITTED), "1" * 40, []
        self.failures = {"state": [], "open": []}

    def state(self, environment):
        if self.failures["state"]:
            raise self.failures["state"].pop()
        return copy.deepcopy(self.data[environment]), self.sha

    def states(self):
        return [copy.deepcopy(s) for _, s in sorted(self.data.items())], self.sha

    def open(self, environment, proposed, *, base_sha, iac_plan_id, plan):
        if self.failures["open"]:
            raise self.failures["open"].pop()
        self.opened.append((environment, proposed, base_sha, iac_plan_id, plan["plan_digest"]))
        return {"number": 700 + len(self.opened), "url": "https://example.invalid/pull", "head_sha": "2" * 40}



def setup(backend, check_request=allow):
    collect(backend.store)
    repository, approvals = Repository(), infra(backend.store)
    return repository, approvals, IacPlanService(backend.store, repository=repository, infra_service=approvals,
                                                 check_access=allow, check_request=check_request)


def approve(approvals, view, key="a1"):
    return approvals.approve(APPROVER, PLAN, binding=view["binding"], expires_at=LATER, command_key=key)["approval_id"]


def test_an_approved_plan_opens_one_desired_state_pull_request(backend):
    repository, approvals, iac = setup(backend)
    view = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket")
    assert view["accepted"] and view["binding"]["requested_by"] == "operator"
    assert view["effects"][0]["anonymous_access"]["after"] == []
    assert iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket") == view
    approval_id = approve(approvals, view)
    receipt = iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approval_id)
    [(environment, proposed, base, plan_id, digest)] = repository.opened
    assert (environment, base, plan_id, digest) == ("target-dev", "1" * 40, view["iac_plan_id"], view["plan_digest"])
    assert proposed["modules"][0]["params"] == CLOSED
    assert receipt["number"] == 701 and receipt["approval_id"] == approval_id
    # A replay returns the same PR; the approval was claimed once, for this plan.
    assert iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approval_id) == receipt
    assert len(repository.opened) == 1
    assert approvals.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] == view["iac_plan_id"]
    assert iac.read(PRINCIPAL, PLAN, view["iac_plan_id"])["pull_request"] == receipt
    with pytest.raises(EventError, match="approval_already_consumed"):
        iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approve(approvals, view, "a2"))


def test_a_moved_desired_state_needs_a_new_plan_and_keeps_the_approval_unclaimed(backend):
    repository, approvals, iac = setup(backend)
    view = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket")
    approval_id = approve(approvals, view)
    repository.data["target-dev"]["modules"][0]["params"]["demo_run_id"] = "demo-2026-11-09"
    with pytest.raises(EventError, match="iac_plan_stale"):
        iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approval_id)
    assert repository.opened == [] and approvals.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] is None


def test_an_approval_of_another_digest_or_by_the_requester_opens_nothing(backend):
    repository, approvals, iac = setup(backend)
    view = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket")
    other = approvals.approve(APPROVER, PLAN, binding=view["binding"] | {"plan_digest": "d" * 64},
                              expires_at=LATER, command_key="other")["approval_id"]
    with pytest.raises(EventError, match="binding_mismatch"):
        iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=other)
    with pytest.raises(EventError, match="self_approval_forbidden"):
        approvals.approve(PRINCIPAL, PLAN, binding=view["binding"], expires_at=LATER, command_key="self")
    assert repository.opened == []


def test_an_approval_opens_only_the_plan_and_requester_it_names(backend):
    repository, approvals, iac = setup(backend)
    view = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket")
    # The requester approves their own plan under another requester's name: approve cannot tell.
    renamed = approvals.approve(PRINCIPAL, PLAN, binding=view["binding"] | {"requested_by": "someone-else"},
                                expires_at=LATER, command_key="renamed")["approval_id"]
    # The same change planned again (another Idempotency-Key) is another plan: A's approval is not B's.
    twin = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket-again")
    assert twin["plan_digest"] == view["plan_digest"] and twin["iac_plan_id"] != view["iac_plan_id"]
    for iac_plan_id, approval_id in ((view["iac_plan_id"], renamed), (twin["iac_plan_id"], approve(approvals, view))):
        with pytest.raises(EventError, match="binding_mismatch"):
            iac.open_pull_request(PRINCIPAL, PLAN, iac_plan_id, approval_id=approval_id)
        assert approvals.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] is None
    assert repository.opened == []


def test_only_an_operator_who_may_apply_infra_becomes_the_requester(backend):
    def apply_infra(*_):
        raise EventError("forbidden")

    repository, _, iac = setup(backend, check_request=apply_infra)
    repository.failures["state"].append(AssertionError("the repository is read before the permission check"))
    with pytest.raises(EventError, match="forbidden"):
        iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                   command_key="close-bucket")


def test_a_repository_failure_is_one_code_and_the_claimed_approval_resumes(backend):
    repository, approvals, iac = setup(backend)
    repository.failures["state"].append(GitHubError(502, "repos/x/contents"))
    with pytest.raises(EventError, match="iac_repository_unavailable"):
        iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                   command_key="close-bucket")
    view = iac.create(PRINCIPAL, PLAN, environment="target-dev", module="target-object-exposure", params=CLOSED,
                      command_key="close-bucket")
    approval_id = approve(approvals, view)
    repository.failures["open"].append(OSError("connection lost before PR"))
    with pytest.raises(EventError, match="iac_repository_unavailable"):
        iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approval_id)
    assert approvals.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] == view["iac_plan_id"]
    receipt = iac.open_pull_request(PRINCIPAL, PLAN, view["iac_plan_id"], approval_id=approval_id)
    assert receipt["number"] == 701 and len(repository.opened) == 1


@pytest.mark.parametrize(("module", "params", "code"), [
    ("no-such-module", {}, "iac_module_not_in_environment"),  # every catalog module may be in target-dev
    ("target-object-exposure", {"state": "public"}, "iac_params_invalid"),
])
def test_invalid_requests_are_refused_before_anything_is_stored(backend, module, params, code):
    _, _, iac = setup(backend)
    with pytest.raises(EventError, match=code):
        iac.create(PRINCIPAL, PLAN, environment="target-dev", module=module, params=params, command_key="bad")
    with pytest.raises(EventError, match="iac_plan_unavailable"):
        iac.read(PRINCIPAL, PLAN, "iac-missing")


def test_operator_routes(backend):
    repository, approvals, iac = setup(backend)
    app = create_cutover_app(service(backend.store, None), resolve_principal=lambda request: PRINCIPAL,
                             infra_service=approvals, iac_service=iac)
    client = TestClient(app)
    created = client.post(f"/v1/plans/{PLAN}/iac/plans", headers={"Idempotency-Key": "k1"},
                          json={"environment": "target-dev", "module": "target-object-exposure", "params": CLOSED})
    assert created.status_code == 201, created.text
    view = created.json()
    assert client.get(f"/v1/plans/{PLAN}/iac/plans/{view['iac_plan_id']}").json()["pull_request"] is None
    approval_id = approve(approvals, view)
    opened = client.post(f"/v1/plans/{PLAN}/iac/plans/{view['iac_plan_id']}/pull-request",
                         json={"approval_id": approval_id})
    assert opened.status_code == 201 and opened.json()["number"] == 701
    bad = client.post(f"/v1/plans/{PLAN}/iac/plans", headers={"Idempotency-Key": "k2"},
                      json={"environment": "../x", "module": "target-object-exposure", "params": CLOSED})
    assert bad.status_code == 422


class FakeApp:
    """The GitHub API calls GitHubIacRepository makes, over an in-memory repository."""

    def __init__(self, files):
        self.repository, self.permissions = "Softbank-Hackathon-Finals/platform", dict(WRITER_PERMISSIONS)
        self.files, self.trees, self.commits, self.refs, self.pulls = files, {}, {}, {"develop": "base"}, []
        self.fail_once = set()

    def call(self, method, path, body=None):
        path = path.removeprefix(f"repos/{self.repository}/")
        if (method, path) in self.fail_once:
            self.fail_once.discard((method, path))
            raise OSError("connection lost")
        if method == "GET" and path.startswith("git/ref/heads/"):
            name = path.removeprefix("git/ref/heads/")
            sha = self.refs.get(name) or self.refs.get(f"refs/heads/{name}")
            if sha is None:
                raise GitHubError(404, path)
            return json.dumps({"object": {"sha": sha}}).encode()
        if method == "GET" and path.startswith("contents/infra/iac/environments?"):
            return json.dumps([{"type": "file", "name": p.rsplit("/", 1)[1]} for p in self.files
                               if p.startswith("infra/iac/environments/")]).encode()
        if method == "GET" and path.startswith("contents/"):
            content = self.files[path.removeprefix("contents/").split("?")[0]]
            return json.dumps({"content": base64.b64encode(content.encode()).decode()}).encode()
        if method == "GET" and path.startswith("pulls?"):
            head = path.split("head=", 1)[1].split("&", 1)[0]
            owner = self.repository.split("/")[0]
            return json.dumps([p for p in self.pulls if f"{owner}:{p['head']['ref']}" == head]).encode()
        if method == "GET" and path.startswith("git/commits/"):
            made = self.commits.get(path.removeprefix("git/commits/"))
            if made:
                return json.dumps({"tree": {"sha": made["tree"]},
                                   "parents": [{"sha": sha} for sha in made["parents"]]}).encode()
            return json.dumps({"tree": {"sha": "tree-base"}}).encode()
        if method == "POST" and path == "git/trees":
            self.trees["tree-new"] = body
            return json.dumps({"sha": "tree-new"}).encode()
        if method == "POST" and path == "git/commits":
            self.commits["c" * 40] = body
            return json.dumps({"sha": "c" * 40}).encode()
        if method == "POST" and path == "git/refs":
            self.refs[body["ref"]] = body["sha"]
            return b"{}"
        if method == "POST" and path == "pulls":
            self.pulls.append({**body, "number": 9, "html_url": "https://example.invalid/9",
                               "head": {"ref": body["head"], "sha": "c" * 40}})
            return json.dumps(self.pulls[-1]).encode()
        raise AssertionError((method, path))


def test_the_pull_request_carries_the_state_its_chart_and_the_policy_fragments():
    files = {f"infra/iac/environments/{name}.json": json.dumps(state) for name, state in COMMITTED.items()}
    app = FakeApp(files)
    repository = GitHubIacRepository(app, app.repository)
    state, sha = repository.state("target-dev")
    proposed = copy.deepcopy(state)
    proposed["modules"][0]["params"] = CLOSED
    plan = {"plan_digest": "e" * 64, "effects": []}
    opened = repository.open("target-dev", proposed, base_sha=sha, iac_plan_id="iac-1", plan=plan)
    assert opened == {"number": 9, "url": "https://example.invalid/9", "head_sha": "c" * 40}
    written = {entry["path"]: entry["content"] for entry in app.trees["tree-new"]["tree"]}
    assert json.loads(written["infra/iac/environments/target-dev.json"]) == proposed
    for name, text in chart_files(proposed).items():
        assert written[f"infra/iac/charts/target-dev/{name}"] == text
    states = [proposed if s["environment"] == "target-dev" else s for _, s in sorted(COMMITTED.items())]
    assert json.loads(written["infra/tailnet/routes-policy.json"]) == fragment(states)
    assert json.loads(written["infra/tailnet/routes-policy.workload.json"]) == fragment(states, "workload")
    # The node bundle (cluster-audit) is regenerated too: its manifest carries the state digest, so any state
    # change moves it, and CI requires the committed bundle to be the generation of the committed state.
    nodes = {k: v for k, v in written.items() if k.startswith("infra/iac/nodes/")}
    assert nodes == {f"infra/iac/nodes/target-dev/{k}": v for k, v in node_files(proposed).items()}
    committed = REPO / "infra/iac/nodes/target-dev"
    assert nodes["infra/iac/nodes/target-dev/manifest.json"] != (committed / "manifest.json").read_text()
    assert nodes["infra/iac/nodes/target-dev/etc/rancher/rke2/audit-policy.yaml"] == (
        committed / "etc/rancher/rke2/audit-policy.yaml").read_text()
    assert not any(k.startswith("infra/iac/nodes/") for k in generated_files(states, "source-dev"))  # no node files
    assert app.refs["refs/heads/bravo-iac/iac-1"] == "c" * 40 and app.pulls[0]["base"] == "develop"
    # Opened before a crash: the same PR comes back and nothing is written again.
    assert repository.open("target-dev", proposed, base_sha=sha, iac_plan_id="iac-1", plan=plan) == opened
    assert len(app.pulls) == 1 and len(app.commits) == 1


def test_the_writer_holds_only_contents_and_pull_requests_on_the_platform_repository():
    app = FakeApp({})
    app.permissions = {"contents": "write", "pull_requests": "write", "actions": "write"}
    with pytest.raises(ValueError, match="iac_writer_permissions_invalid"):
        GitHubIacRepository(app, app.repository)
    app.permissions = dict(WRITER_PERMISSIONS)
    with pytest.raises(ValueError, match="iac_writer_permissions_invalid"):
        GitHubIacRepository(app, "Softbank-Hackathon-Finals/demo-chat")


def test_a_branch_pushed_before_a_failed_pull_request_is_reused_only_if_it_is_this_change():
    files = {f"infra/iac/environments/{name}.json": json.dumps(state) for name, state in COMMITTED.items()}
    app = FakeApp(files)
    repository = GitHubIacRepository(app, app.repository)
    state, sha = repository.state("target-dev")
    proposed = copy.deepcopy(state)
    proposed["modules"][0]["params"] = CLOSED
    plan = {"plan_digest": "e" * 64, "effects": []}
    app.fail_once.add(("POST", "pulls"))
    with pytest.raises(OSError):
        repository.open("target-dev", proposed, base_sha=sha, iac_plan_id="iac-1", plan=plan)
    assert app.refs["refs/heads/bravo-iac/iac-1"] == "c" * 40 and app.pulls == []
    # The retry finds the pushed branch, checks its parent and tree, and opens the PR for it.
    opened = repository.open("target-dev", proposed, base_sha=sha, iac_plan_id="iac-1", plan=plan)
    assert opened["head_sha"] == "c" * 40 and len(app.pulls) == 1 and len(app.commits) == 1
    # A branch with the same name but another tree (or base) is never opened as this change.
    app.refs["refs/heads/bravo-iac/iac-2"] = "c" * 40
    app.commits["c" * 40]["tree"] = "tree-other"
    with pytest.raises(EventError, match="iac_branch_conflict"):
        repository.open("target-dev", proposed, base_sha=sha, iac_plan_id="iac-2", plan=plan)
    assert len(app.pulls) == 1
