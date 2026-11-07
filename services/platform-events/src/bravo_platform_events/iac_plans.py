"""T44 destination IaC through the platform API (ADR-0032): plan, T10 infra approval, desired-state PR.

On a migration plan an operator asks for a change of one module's params in one environment. The
platform plans it against the desired state committed on the base branch (bravo_iac, deterministic),
a different human approves that plan_digest through the existing T10 `approval_kind=infra`, and only
then the platform opens a pull request with the new desired state, its generated chart and tailnet
policy fragments. The approval is claimed once (run_id = the IaC plan id) and only if the plan
recomputed against the base branch *now* has the approved digest. Nothing here merges, applies to a
cluster or holds cluster credentials: review and merge stay in Git, the cluster's Argo CD applies a
reviewed commit.

The repository port is injected: `state(environment) -> (state, base_sha)` and
`open(environment, proposed, *, base_sha, iac_plan_id, plan) -> {number, url, head_sha}`.
`GitHubIacRepository` implements it with the platform's GitHub App installation token.
"""

import base64
import hashlib
import json

from bravo_iac import catalog, chart, node, tailnet
from bravo_iac.modules import IacError, tailnet_of
from bravo_iac.plan import plan as iac_plan

from .contract import validate_identifier
from .cutover import _get, _put
from .github_app import GitHubError
from .plan_api import Principal, authorized_plan
from .plans import _canonical
from .store import EventError

KEY = "iac-plan-{}"
PULL = "iac-pull-request-{}"
ENVIRONMENTS = "infra/iac/environments"
# Exactly what the writer may hold on the platform repository (github_app refuses wider or narrower).
WRITER_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


def _proposed(state, module_id, params):
    if not any(entry["id"] == module_id for entry in state["modules"]):
        raise IacError("IAC_MODULE_NOT_IN_ENVIRONMENT")
    return dict(state, modules=[dict(entry, params=params) if entry["id"] == module_id else entry
                                for entry in state["modules"]])


def _view(value):
    plan = value["plan"]
    return {"iac_plan_id": value["iac_plan_id"], "environment": value["environment"], "module": value["module"],
            "base_sha": value["base_sha"], "plan_digest": plan["plan_digest"], "accepted": plan["accepted"],
            "refusals": plan["refusals"], "effects": plan["effects"], "changes": len(plan["changes"]),
            "requested_by": value["requested_by"],
            # What a T10 infra approval binds (POST /v1/plans/{plan_id}/approvals, approval_kind=infra).
            "binding": {"infra_plan_id": value["iac_plan_id"], "plan_digest": plan["plan_digest"],
                        "requested_by": value["requested_by"]}}


class IacPlanService:
    def __init__(self, store, *, repository, infra_service, check_access, check_request):
        if (infra_service is None or infra_service.store is not store
                or not callable(check_access) or not callable(check_request)):
            raise ValueError("iac_plan_configuration_invalid")
        self.store, self.repository, self.infra = store, repository, infra_service
        self.check_access, self.check_request = check_access, check_request

    def _plan(self, environment, module_id, params):
        try:
            state, base_sha = self.repository.state(environment)
        except (GitHubError, OSError):
            raise EventError("iac_repository_unavailable") from None
        if state["environment"] != environment:
            raise IacError("IAC_STATE_UNREADABLE")
        # API requests come from operators; an agent plans through its own tool (infra.plan).
        return state, base_sha, iac_plan(state, _proposed(state, module_id, params), origin="operator")

    def create(self, principal, plan_id, *, environment, module, params, command_key):
        validate_identifier(command_key)
        if not isinstance(principal, Principal):
            raise EventError("forbidden")
        # The requester (recorded as requested_by) must be able to apply infra, not only read the plan.
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_request)
        try:
            _, base_sha, plan = self._plan(environment, module, params)
        except IacError as error:
            raise EventError(error.code.lower()) from None
        iac_plan_id = "iac-" + hashlib.sha256(f"{plan_id}\0{command_key}".encode()).hexdigest()[:24]
        request = dict(environment=environment, module=module, params=params, actor_id=principal.subject_id)
        value = dict(iac_plan_id=iac_plan_id, environment=environment, module=module, params=params,
                     base_sha=base_sha, plan=plan, requested_by=principal.subject_id)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            authorized_plan(c, principal, plan_id, self.check_request)
            old = _get(c, plan_id, KEY.format(iac_plan_id))
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return _view(old[1])
            _put(c, plan_id, KEY.format(iac_plan_id), request, value)
        return _view(value)

    def read(self, principal, plan_id, iac_plan_id):
        validate_identifier(iac_plan_id)
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_access)
            saved = _get(c, plan_id, KEY.format(iac_plan_id))
            if saved is None:
                raise EventError("iac_plan_unavailable")
            pull = _get(c, plan_id, PULL.format(iac_plan_id))
        return _view(saved[1]) | {"pull_request": pull[1] if pull else None}

    def open_pull_request(self, principal, plan_id, iac_plan_id, *, approval_id):
        """Claim the approval for this plan (once) and open the desired-state PR; a replay returns it."""
        validate_identifier(iac_plan_id)
        with self.store._transaction() as c:
            authorized_plan(c, principal, plan_id, self.check_access)
            saved = _get(c, plan_id, KEY.format(iac_plan_id))
            if saved is None:
                raise EventError("iac_plan_unavailable")
            done = _get(c, plan_id, PULL.format(iac_plan_id))
        value = saved[1]
        if done:
            if done[0]["approval_id"] != approval_id:
                raise EventError("approval_already_consumed")
            return done[1]
        if not value["plan"]["accepted"]:
            raise EventError("iac_plan_refused")
        try:
            state, base_sha, plan = self._plan(value["environment"], value["module"], value["params"])
        except IacError as error:
            raise EventError(error.code.lower()) from None
        if plan["plan_digest"] != value["plan"]["plan_digest"]:
            raise EventError("iac_plan_stale")  # the committed state moved: plan again, approve again
        # apply_infra now, the approver still holds approve_infra, and the approval is of this plan as
        # recorded: its id, its requester and its (recomputed) digest.
        self.infra.claim(principal, plan_id, approval_id, run_id=iac_plan_id,
                         binding=_view(value)["binding"] | {"plan_digest": plan["plan_digest"]})
        try:
            opened = self.repository.open(value["environment"], _proposed(state, value["module"], value["params"]),
                                          base_sha=base_sha, iac_plan_id=iac_plan_id, plan=plan)
        except (GitHubError, OSError):
            # Claimed already: a retry with the same approval resumes the same branch (see open).
            raise EventError("iac_repository_unavailable") from None
        receipt = dict(iac_plan_id=iac_plan_id, approval_id=approval_id, base_sha=base_sha,
                       plan_digest=plan["plan_digest"], number=opened["number"], url=opened["url"],
                       head_sha=opened["head_sha"], opened_by=principal.subject_id)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            old = _get(c, plan_id, PULL.format(iac_plan_id))
            if old is None:
                _put(c, plan_id, PULL.format(iac_plan_id), {"approval_id": approval_id}, receipt)
            elif old[1] != receipt:
                raise EventError("iac_pull_request_conflict")
        return receipt


def generated_files(states, environment):
    """path -> content the repository must hold after `environment`'s state changes (CI checks them)."""
    by_env = {s["environment"]: s for s in states}
    state = by_env[environment]
    files = {f"{ENVIRONMENTS}/{environment}.json": json.dumps(state, indent=2, ensure_ascii=False) + "\n"}
    files |= {f"infra/iac/charts/{environment}/{name}": text for name, text in chart.files(state).items()}
    bundle = node.files(state)
    if json.loads(bundle["manifest.json"])["files"]:  # node files (cluster-audit): the bundle the node applier reads
        files |= {f"infra/iac/nodes/{environment}/{path}": text for path, text in bundle.items()}
    keys = {"default"} | {tailnet_of(g) for s in states for m in catalog.check_state(s)["modules"]
                          if m["id"] == "tailnet-egress" for g in m["params"]["groups"]}
    for key in sorted(keys):
        name = "routes-policy.json" if key == "default" else f"routes-policy.{key}.json"
        files[f"infra/tailnet/{name}"] = json.dumps(tailnet.fragment(states, key), indent=2) + "\n"
    return files


class GitHubIacRepository:
    """The platform repository through one GitHub App installation (contents + pull requests only)."""

    def __init__(self, app, repository, *, base="develop"):
        if app.repository != repository or dict(app.permissions) != WRITER_PERMISSIONS:
            raise ValueError("iac_writer_permissions_invalid")
        self.app, self.repository, self.base = app, repository, base

    def _json(self, method, path, body=None):
        return json.loads(self.app.call(method, f"repos/{self.repository}/{path}", body) or b"null")

    def _base_sha(self):
        return self._json("GET", f"git/ref/heads/{self.base}")["object"]["sha"]

    def _file(self, path, sha):
        value = self._json("GET", f"contents/{path}?ref={sha}")
        return json.loads(base64.b64decode(value["content"]))

    def state(self, environment):
        validate_identifier(environment)
        sha = self._base_sha()
        return catalog.check_state(self._file(f"{ENVIRONMENTS}/{environment}.json", sha)), sha

    def states(self, sha=None):
        sha = sha or self._base_sha()
        names = sorted(e["name"] for e in self._json("GET", f"contents/{ENVIRONMENTS}?ref={sha}")
                       if e["type"] == "file" and e["name"].endswith(".json"))
        return [catalog.check_state(self._file(f"{ENVIRONMENTS}/{name}", sha)) for name in names], sha

    def _ref(self, branch):
        try:
            return self._json("GET", f"git/ref/heads/{branch}")["object"]["sha"]
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def open(self, environment, proposed, *, base_sha, iac_plan_id, plan):
        branch = f"bravo-iac/{iac_plan_id}"
        owner = self.repository.split("/")[0]
        pulls = self._json("GET", f"pulls?head={owner}:{branch}&state=all")
        if pulls:  # opened before a crash: the same branch and PR, never a second one
            pull = pulls[0]
            return {"number": pull["number"], "url": pull["html_url"], "head_sha": pull["head"]["sha"]}
        states, _ = self.states(base_sha)
        states = [proposed if s["environment"] == environment else s for s in states]
        tree = self._json("GET", f"git/commits/{base_sha}")["tree"]["sha"]
        new_tree = self._json("POST", "git/trees", {"base_tree": tree, "tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": text}
            for path, text in sorted(generated_files(states, environment).items())]})["sha"]
        commit = self._ref(branch)
        if commit is None:
            commit = self._json("POST", "git/commits", {
                "tree": new_tree, "parents": [base_sha],
                "message": f"[T44] {environment} 원하는 상태: {plan['plan_digest'][:16]} ({iac_plan_id})"})["sha"]
            self._json("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        else:  # pushed before a failure: open the PR for it only if it is exactly this change
            existing = self._json("GET", f"git/commits/{commit}")
            if existing["tree"]["sha"] != new_tree or [p["sha"] for p in existing["parents"]] != [base_sha]:
                raise EventError("iac_branch_conflict")
        body = ("T10 infra 승인을 받은 IaC 계획의 원하는 상태입니다(T44, ADR-0032). 병합해도 클러스터에는 바로 적용되지 않고, "
                "검토한 커밋 SHA를 Argo에 지정할 때 적용됩니다.\n\n"
                f"- 환경: `{environment}`\n- plan_digest: `{plan['plan_digest']}`\n- IaC 계획: `{iac_plan_id}`\n"
                f"- effects:\n```json\n{json.dumps(plan['effects'], indent=2, ensure_ascii=False)}\n```\n")
        pull = self._json("POST", "pulls", {"head": branch, "base": self.base,
                                            "title": f"[T44] {environment} 원하는 상태 변경 ({iac_plan_id})",
                                            "body": body})
        return {"number": pull["number"], "url": pull["html_url"], "head_sha": commit}
