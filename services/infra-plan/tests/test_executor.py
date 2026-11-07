"""T45 executor over a real Git configuration repository and a simulated cluster (ADR-0033 3.4)."""

import json
import subprocess
import threading

import pytest
from test_plan import REPO, REQUEST, STACK, FakeCharts

from bravo_infra_plan import Planner, StackError, load_stack, parse_stack
from bravo_infra_plan.adapters import (
    HOST_PATHS_ANNOTATION,
    ClusterChecks,
    FileRunStore,
    GitConfigRepo,
    expected_images,
    host_paths_digest,
)
from bravo_infra_plan.applications import application_name
from bravo_infra_plan.executor import Executor, StepOutcome

REV = "1" * 40
PLAN_ID, REQUESTER = "infra-1", "agent-run-1"  # the approved binding besides the digest
NODE = REQUEST["params"]["node_name"]


class FakeCluster:
    """Argo CD reports an Application synced at the revision it was given; resources become ready."""

    OPERATOR_SECRETS = {("tailscale-system", "operator-oauth"), ("target-dev", "target-seaweedfs-auth"),
                        ("bravo-secrets", "target-openbao-tls"), ("target-dev", "bravo-app-metrics-client")}

    def __init__(self, *, never_ready=()):
        self.lock = threading.Lock()
        self.applied, self.helm, self.apps = [], {}, {}
        self.secrets = {key: ("Opaque", {"made": "by the operator"}) for key in self.OPERATOR_SECRETS}
        # The running Tailscale operator's own state after it logged in (ipn kubestore keys).
        self.secrets[("tailscale-system", "operator")] = ("Opaque", {"_current-profile": "x", "_machinekey": "x"})
        self.labelled = set()
        self.never_ready = set(never_ready)
        self.diff_calls = 0
        self.annotations, self.images = {}, set()

    def prepare_node(self):
        """What the platform's node preparation leaves behind: the host-paths attestation and the image."""
        steps = load_stack(STACK).steps
        self.annotations[HOST_PATHS_ANNOTATION] = host_paths_digest(steps["host-dirs"]["expect"]["host_paths"])
        for step in ("openbao-image", "app-image"):
            self.images |= set(expected_images(steps[step]["expect"], REQUEST["params"]))

    def identity(self, node_name):
        return {"cluster_uid": "cluster-1", "node_uid": f"uid-{node_name}"}

    def node_ready(self, node_name):
        return True

    def node_annotation(self, node_name, key):
        return self.annotations.get(key)

    def node_image_names(self, node_name):
        return set(self.images)

    def diff(self, items):
        with self.lock:
            self.diff_calls += 1
            return any(json.dumps(i, sort_keys=True) not in self.applied for i in items)

    def apply(self, items):
        with self.lock:
            for item in items:
                self.applied.append(json.dumps(item, sort_keys=True))
                if item["kind"] == "Application":
                    self.apps[item["metadata"]["name"]] = item["spec"]["source"]["targetRevision"]

    def secret_exists(self, namespace, name):
        return (namespace, name) in self.secrets

    def create_secret(self, namespace, name, secret_type, values):
        assert (namespace, name) not in self.secrets
        self.secrets[(namespace, name)] = (secret_type, dict(values))

    def condition(self, kind, namespace, name, condition):
        return (kind, name) not in self.never_ready

    def application_status(self, name):
        revision = self.apps.get(name)
        return revision and {"revision": revision, "sync": "Synced", "health": "Healthy"}

    def deployments_available(self, namespace):
        return True

    def statefulset_ready(self, namespace, name):
        return (namespace, name) == ("bravo-iac-gitops", "bravo-iac-gitops-application-controller")

    def labelled_secret_exists(self, namespace, selector):
        return True

    seaweedfs_shape = {"bootstrap": ["demo-chat-attachments"], "client": ["demo-chat-attachments"],
                       "anonymous": ["demo-chat-attachments"]}  # T44 target-dev.json is open-demo

    def secret_keys(self, namespace, name):
        found = self.secrets.get((namespace, name))
        return None if found is None else sorted(found[1])  # the adapter's contract: a sorted list of key names

    def secret_identities(self, namespace, name, key):
        return dict(self.seaweedfs_shape) if (namespace, name) in self.secrets else None

    def helm_digest(self, release, namespace):
        return self.helm.get((release, namespace))

    def helm_install(self, chart_dir, values_file, *, release, namespace, digest):
        assert chart_dir.is_dir() and values_file.is_file()
        self.helm[(release, namespace)] = digest


class FakeApprovals:
    def __init__(self):
        self.approvals, self.claims, self.claimed_bindings = {}, {}, []
        self.deny_claims = False

    def approve(self, approval_id, digest, *, infra_plan_id=PLAN_ID, requested_by=REQUESTER):
        binding = {"infra_plan_id": infra_plan_id, "plan_digest": digest, "requested_by": requested_by}
        self.approvals[approval_id] = {"approval_id": approval_id, "binding": binding,
                                       "revoked": False, "claimed_run_id": None, "actor_id": "approver"}

    def read(self, plan_id, approval_id):
        return dict(self.approvals[approval_id], claimed_run_id=self.claims.get(approval_id))

    def claim(self, plan_id, approval_id, *, run_id, binding):
        if self.deny_claims:
            raise StackError("forbidden")
        self.claimed_bindings.append(binding)
        if self.approvals[approval_id]["binding"] != binding:  # T10 compares the whole binding
            raise StackError("binding_mismatch")
        assert self.claims.setdefault(approval_id, run_id) == run_id
        return {"approved_by": "approver", "claimed_by": "applier", "claimed_at": "t"}


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "config.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("destination configuration\n")
    _git("add", ".", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed", cwd=seed)
    _git("push", "-q", str(remote), "main", cwd=seed)
    cluster, approvals, ledger = FakeCluster(), FakeApprovals(), []
    stack = load_stack(STACK)
    planner = Planner(stack, REPO, chart_renderer=FakeCharts())
    repo = GitConfigRepo(str(remote), "main", tmp_path / "work")

    def executor(cluster=cluster, **kw):
        ticks = iter(range(0, 10**9, 5))  # each poll advances 5 s: stable waits pass without real time
        kw.setdefault("monotonic", lambda: next(ticks))
        return Executor(planner, stack_revision=REV, cluster=cluster, repo=repo, approvals=approvals,
                        checks=ClusterChecks(cluster, join_wait_s=0), store=FileRunStore(tmp_path / "runs"),
                        repo_url="file://config", sleep=lambda s: None, **({"ledger": ledger.append} | kw))

    def approve(approval_id):
        digest = planner.plan(REQUEST, stack_revision=REV, target=cluster.identity(NODE),
                              base_commit=repo.head()).plan_digest
        approvals.approve(approval_id, digest)
        return digest

    return dict(executor=executor, approve=approve, cluster=cluster, approvals=approvals, repo=repo,
                remote=remote, ledger=ledger, tmp=tmp_path)


def start(world, run_id, approval_id="appr-1", **kw):
    return world["executor"](**kw).start(request=REQUEST, migration_plan_id="plan-demo", approval_id=approval_id,
                                         run_id=run_id, started_by="applier", infra_plan_id=PLAN_ID,
                                         requested_by=REQUESTER)


def test_a_plan_other_than_the_approved_digest_is_refused_before_anything_changes(world):
    world["approvals"].approve("appr-1", "f" * 64)
    head = world["repo"].head()
    run = start(world, "run-1")
    assert (run["status"], run["reason"]) == ("REFUSED", "plan_digest_mismatch")
    assert world["approvals"].claims == {} and world["repo"].head() == head
    assert world["cluster"].applied == [] and world["cluster"].helm == {}


@pytest.mark.parametrize("field,other", [("infra_plan_id", "infra-2"), ("requested_by", "agent-run-2")])
def test_an_approval_of_the_same_digest_for_another_plan_or_requester_is_refused(world, field, other):
    """#603 review: the approved digest alone is not the approval of this plan record."""
    digest = world["approve"]("appr-1")
    world["approvals"].approve("appr-1", digest, **{"infra_plan_id": PLAN_ID, "requested_by": REQUESTER,
                                                    field: other})
    head = world["repo"].head()
    run = start(world, "run-1")
    assert (run["status"], run["reason"]) == ("REFUSED", "binding_mismatch")
    assert world["approvals"].claims == {} and world["approvals"].claimed_bindings == []
    assert world["repo"].head() == head and world["cluster"].applied == []


def test_start_and_resume_claim_with_the_whole_binding(world):
    digest = world["approve"]("appr-1")
    assert start(world, "run-1")["status"] == "WAITING_OPERATOR"
    world["cluster"].prepare_node()
    assert world["executor"]().resume("run-1")["status"] == "SUCCEEDED"
    whole = {"infra_plan_id": PLAN_ID, "plan_digest": digest, "requested_by": REQUESTER}
    assert world["approvals"].claimed_bindings == [whole, whole]


def test_a_run_stored_without_its_binding_holds_on_resume(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    path = world["tmp"] / "runs" / "run-1.json"
    stored = json.loads(path.read_text())
    del stored["binding"]
    path.write_text(json.dumps(stored))
    run = world["executor"]().resume("run-1")
    assert (run["status"], run["reason"]) == ("HOLD", "run_binding_missing")


def test_parallel_lines_run_until_human_steps_then_confirmations_finish_the_run(world):
    world["approve"]("appr-1")
    run = start(world, "run-1")
    states = {k: v["status"] for k, v in run["steps"].items()}
    assert run["status"] == "WAITING_OPERATOR"
    assert states["host-dirs"] == states["openbao-image"] == "WAITING_OPERATOR"  # the node is not prepared yet
    # Lines that do not need the host directories or the OpenBao image went ahead in parallel.
    assert states["argocd"] == "SKIPPED"  # T44 installed it; T45 only observes it
    for done in ("base", "base-sync", "cnpg-sync", "eso-sync", "tailscale-sync", "db-secrets"):
        assert states[done] in ("APPLIED", "SKIPPED"), done
    for blocked in ("database", "objects", "openbao", "app", "verify"):
        assert states[blocked] == "PENDING", blocked

    world["cluster"].prepare_node()
    run = world["executor"]().resume("run-1")
    assert run["status"] == "SUCCEEDED", {k: v for k, v in run["steps"].items() if v["status"] != "APPLIED"}
    assert run["steps"]["host-dirs"]["status"] == run["steps"]["openbao-image"]["status"] == "SKIPPED"
    assert all("started_at" in s and "ended_at" in s for s in run["steps"].values())

    # Every published path is in Git, and Argo CD points at the commit that published it.
    tree = world["repo"].read_tree("")
    assert {"base/accounts.json", "openbao/server.json", "app/application.json"} <= set(tree)
    assert "migration.json" not in {name.split("/")[-1] for name in tree}
    for path, commit in run["commits"].items():
        assert world["cluster"].apps[application_name("target-onprem", path)] == commit
    assert world["cluster"].helm == {}  # no chart install: the destination Argo CD is T44's


def test_secret_values_stay_out_of_records_and_existing_secrets_are_never_replaced(world):
    world["cluster"].secrets[("target-dev", "target-replication")] = ("kubernetes.io/basic-auth", {"password": "kept"})
    world["approve"]("appr-1")
    start(world, "run-1")
    created = world["cluster"].secrets[("target-dev", "target-app-db")][1]
    assert created["username"] == "chat_app" and len(created["password"]) == 32
    assert world["cluster"].secrets[("target-dev", "target-replication")][1] == {"password": "kept"}
    stored = (world["tmp"] / "runs" / "run-1.json").read_text() + json.dumps(world["ledger"])
    assert created["password"] not in stored


def test_a_second_approved_run_changes_nothing(world):
    world["approve"]("appr-1")
    world["cluster"].prepare_node()
    assert start(world, "run-1")["status"] == "SUCCEEDED"
    head, applied, secrets = world["repo"].head(), len(world["cluster"].applied), dict(world["cluster"].secrets)

    world["approve"]("appr-2")  # a new plan: the base commit moved, so the digest is new
    run = start(world, "run-2", approval_id="appr-2")
    assert run["status"] == "SUCCEEDED"
    changed = {k: v["status"] for k, v in run["steps"].items() if v["status"] == "APPLIED"}
    # Only steps that re-check by observation (probes, waits, syncs) report APPLIED.
    assert {world_kind(k) for k in changed} <= {"probe", "gitops_sync", "wait"}, changed
    assert world["repo"].head() == head and len(world["cluster"].applied) == applied
    assert world["cluster"].secrets == secrets


def world_kind(step_id):
    return load_stack(STACK).steps[step_id]["kind"]


def test_a_wait_that_never_converges_holds_only_its_dependents(world):
    ticks = iter(range(0, 10**6, 100))
    cluster = FakeCluster(never_ready={("Cluster", "target-db")})
    world["approvals"].approve("appr-1", world["executor"](cluster=cluster).planner.plan(
        REQUEST, stack_revision=REV, target=cluster.identity(NODE), base_commit=world["repo"].head()).plan_digest)
    executor = world["executor"](cluster=cluster, monotonic=lambda: next(ticks))
    run = executor.start(request=REQUEST, migration_plan_id="plan-demo", approval_id="appr-1",
                         run_id="run-1", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    cluster.prepare_node()
    run = executor.resume("run-1")
    states = {k: v for k, v in run["steps"].items()}
    assert run["status"] == "HOLD" and states["database-ready"]["reason"] == "wait_timeout"
    assert states["migration"]["status"] == "PENDING" and states["app"]["status"] == "PENDING"
    assert states["delivery-ready"]["status"] == "APPLIED" and states["objects-sync"]["status"] == "APPLIED"


def test_a_moved_configuration_repository_holds_a_resumed_run(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    other = world["tmp"] / "other"
    _git("clone", "-q", str(world["remote"]), str(other))
    (other / "base").mkdir(exist_ok=True)
    (other / "base" / "intruder.json").write_text("{}\n")
    _git("add", ".", cwd=other)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "elsewhere", cwd=other)
    _git("push", "-q", "origin", "main", cwd=other)
    run = world["executor"]().confirm("run-1", "host-dirs", confirmed_by="operator")
    assert (run["status"], run["reason"]) == ("HOLD", "config_repo_moved")


def test_an_observable_human_step_does_not_pass_on_a_confirmation_alone(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    executor = world["executor"]()
    run = executor.confirm("run-1", "host-dirs", confirmed_by="operator")
    assert (run["steps"]["host-dirs"]["status"], run["steps"]["host-dirs"]["reason"]) == (
        "WAITING_OPERATOR", "confirm_check_failed")
    # An attestation for other directories than the step declares does not count either.
    world["cluster"].annotations[HOST_PATHS_ANNOTATION] = host_paths_digest([{"path": "/var/lib/x", "uid": 0,
                                                                             "gid": 0, "mode": "0755"}])
    assert executor.resume("run-1")["steps"]["host-dirs"]["status"] == "WAITING_OPERATOR"
    world["cluster"].prepare_node()
    assert executor.resume("run-1")["status"] == "SUCCEEDED"


def test_the_stack_names_the_openbao_images_the_renderer_deploys():
    import importlib.util
    spec = importlib.util.spec_from_file_location("target_secrets_render", REPO / "infra/envs/target-secrets/render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    expect = load_stack(STACK).steps["openbao-image"]["expect"]
    for arch, pinned in render.BAO_IMAGES.items():  # render.py --arch (#682) picks the same one
        repo_tag, digest = pinned.split("@", 1)
        assert set(expected_images(expect, {"arch": arch})) == {repo_tag, f"{repo_tag.rsplit(':', 1)[0]}@{digest}"}, arch
    with pytest.raises(StackError, match="image_unavailable_for_arch"):
        expected_images({"images_by_arch": {"amd64": ["x"]}}, {"arch": "arm64"})

def test_an_approval_already_claimed_by_another_run_is_refused(world):
    world["approve"]("appr-1")
    world["approvals"].claims["appr-1"] = "run-0"
    run = start(world, "run-1")
    assert (run["status"], run["reason"]) == ("REFUSED", "approval_unusable")


# --- regressions from the independent review (2026-11-07) ------------------------------------

def test_a_switched_cluster_holds_every_remaining_step(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    applied = len(world["cluster"].applied)
    other = FakeCluster()
    other.identity = lambda node: {"cluster_uid": "other-cluster", "node_uid": f"uid-{node}"}
    run = world["executor"](cluster=other).confirm("run-1", "host-dirs", confirmed_by="operator")
    assert run["status"] == "HOLD"
    assert {s.get("reason") for s in run["steps"].values() if s["status"] == "HOLD"} == {"target_identity_changed"}
    assert other.applied == [] and len(world["cluster"].applied) == applied


def test_a_resumed_run_executes_the_stored_stack_not_the_edited_file(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    edited = json.loads(STACK.read_text())
    for step in edited["steps"]:
        if step["id"] == "database":
            step["needs"].remove("host-dirs")
    planner = Planner(parse_stack(edited), REPO, chart_renderer=FakeCharts())
    executor = world["executor"]()
    executor.planner, executor.stack = planner, planner.stack
    run = executor.resume("run-1")
    assert run["steps"]["host-dirs"]["status"] == "WAITING_OPERATOR"
    assert run["steps"]["database"]["status"] == "PENDING"  # the approved order still holds

    record = world["tmp"] / "runs" / "run-1.json"
    tampered = json.loads(record.read_text())
    tampered["stack"]["steps"][0]["description"] = "changed after approval"
    record.write_text(json.dumps(tampered))
    assert world["executor"]().resume("run-1")["reason"] == "stored_stack_mismatch"


def test_a_revoked_approval_or_a_caller_without_apply_grant_stops_a_resume(world):
    world["approve"]("appr-1")
    start(world, "run-1")
    world["approvals"].approvals["appr-1"]["revoked"] = True
    run = world["executor"]().confirm("run-1", "host-dirs", confirmed_by="operator")
    assert (run["status"], run["reason"]) == ("HOLD", "approval_revoked")
    assert run["steps"]["database"]["status"] == "PENDING"

    world["approvals"].approvals["appr-1"]["revoked"] = False
    world["approvals"].deny_claims = True  # T10 refuses this caller's apply_infra
    with pytest.raises(StackError, match="forbidden"):
        world["executor"]().resume("run-1")


def test_a_publish_holds_when_the_repository_moved_even_if_the_path_looks_unchanged(world):
    world["approve"]("appr-1")
    run = start(world, "run-1")
    executor = world["executor"]()
    stale = dict(run, head=_git("rev-parse", "HEAD~1", cwd=world["tmp"] / "work"), commits={})
    step = executor.stack.steps["cnpg"]
    world["cluster"].apps.pop(application_name("target-onprem", "cnpg"), None)
    with pytest.raises(StepOutcome) as held:
        executor._gitops_publish(stale, step)
    assert held.value.reason == "config_repo_moved"


def test_replace_tree_commits_only_its_path(world):
    repo = world["repo"]
    head = repo.head()
    (world["tmp"] / "work" / "other").mkdir(exist_ok=True)
    (world["tmp"] / "work" / "other" / "x.json").write_text("{}\n")
    _git("add", "other/x.json", cwd=world["tmp"] / "work")
    commit = repo.replace_tree("base", {"base/a.json": "{}\n"}, expected_head=head, message="t")
    changed = _git("show", "--name-only", "--format=", commit, cwd=world["tmp"] / "work").splitlines()
    assert changed == ["base/a.json"]


def test_an_uncomparable_diff_holds_instead_of_applying(world):
    class Blind(FakeCluster):
        def diff(self, items):
            raise RuntimeError("kubectl diff failed")
    cluster = Blind()
    world["approvals"].approve("appr-1", world["executor"](cluster=cluster).planner.plan(
        REQUEST, stack_revision=REV, target=cluster.identity(NODE), base_commit=world["repo"].head()).plan_digest)
    run = world["executor"](cluster=cluster).start(request=REQUEST, migration_plan_id="plan-demo",
                                                   approval_id="appr-1", run_id="run-1", started_by="applier",
                                                   infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    assert run["steps"]["base"]["status"] == "HOLD" and cluster.applied == []


def test_run_ids_cannot_leave_the_runs_directory(world):
    with pytest.raises(StackError, match="invalid_run_id"):
        FileRunStore(world["tmp"] / "runs").load("../../x")


def test_a_wait_needs_its_condition_to_hold_for_the_stable_period(world):
    clock = iter(range(0, 10**6, 5))
    flips = iter([True, False, True, True, True, True, True, True])

    class Flaky(FakeCluster):
        def condition(self, kind, namespace, name, condition):
            return next(flips, True)

    executor = world["executor"](cluster=Flaky(), monotonic=lambda: next(clock))
    step = {"id": "w", "kind": "wait", "owner": "T30", "description": "d", "timeout_seconds": 300,
            "stable_seconds": 20, "target": {"kind": "Cluster", "namespace": "n", "name": "x"}, "condition": "Ready"}
    seen = []
    executor.sleep = lambda s: seen.append(s)
    assert executor._wait({}, step) == "APPLIED"
    assert len(seen) >= 4  # the False reset the stable window


def test_object_secrets_must_match_t44_desired_state(world):
    world["cluster"].seaweedfs_shape = {"bootstrap": ["demo-chat-attachments"], "client": ["demo-chat-attachments"]}
    world["approve"]("appr-1")  # T44 wants open-demo; the operator made a closed s3.json
    run = start(world, "run-1")
    assert run["steps"]["object-secrets"]["status"] == "WAITING_OPERATOR"
    entry = next(s for s in run["plan"]["steps"] if s["id"] == "object-secrets")
    assert entry["desired"] == {"state": "open-demo", "bucket": "demo-chat-attachments",
                                "demo_run_id": "demo-2026-11-08"}
    base = json.loads(run["files"]["base"]["base/tailscale-namespace.json"])["items"][0]
    assert base["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"


def test_runs_are_chained_into_the_t24_ledger_and_resume_after_a_restart(world):
    from bravo_audit.reader import read_local_run

    from bravo_infra_plan.adapters import AuditLedger

    store_root = world["tmp"] / "audit"
    runs = FileRunStore(world["tmp"] / "runs")
    world["approve"]("appr-1")
    executor = world["executor"](ledger=AuditLedger(str(store_root), runs))
    executor.start(request=REQUEST, migration_plan_id="plan-demo", approval_id="appr-1", run_id="run-1",
                   started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    first = read_local_run(store_root, "plan-demo", "run-1")
    assert first.record_count > 3 and {r["source"] for r in first.records} == {"infra"}
    restarted = world["executor"](ledger=AuditLedger(str(store_root), runs))  # a new process
    restarted.confirm("run-1", "host-dirs", confirmed_by="operator")
    after = read_local_run(store_root, "plan-demo", "run-1")
    assert after.record_count > first.record_count
    assert [r["sha256"] for r in after.records[:first.record_count]] == [r["sha256"] for r in first.records]


def test_the_t45_project_allows_only_what_the_approved_manifests_make(world):
    from bravo_infra_plan.applications import project

    world["approve"]("appr-1")
    run = start(world, "run-1")
    published = {k: v for k, v in run["files"].items() if k != "migration"}
    spec = project("target-onprem", repo_url="file://config", files=published)["spec"]
    namespaces = {d["namespace"] for d in spec["destinations"]}
    assert {"target-dev", "bravo-secrets", "external-secrets", "cnpg-system", "tailscale-system"} <= namespaces
    assert "*" not in namespaces and "bravo-iac-gitops" not in namespaces and "kube-system" not in namespaces
    kinds = {(r["group"], r["kind"]) for r in spec["clusterResourceWhitelist"]}
    assert ("", "Namespace") in kinds and ("", "PersistentVolume") in kinds
    # (fake charts here render ConfigMaps only; real charts with include_crds add their CRDs the same way)
    assert ("*", "*") not in kinds
    assert not any(kind == "ProxyGroup" for _, kind in kinds)  # T44's tailnet objects stay T44's
    # Argo CD refuses an Application whose destination namespace the project does not list
    # (an empty one matches only "*"): each names a namespace its own manifests use.
    for item in world["cluster"].applied:
        app = json.loads(item)
        if app["kind"] == "Application":
            assert app["spec"]["destination"]["namespace"] in namespaces, app["metadata"]["name"]


def test_an_isolated_target_runs_to_verify_with_the_tailscale_line_omitted(world):
    request = json.loads(json.dumps(REQUEST))
    request["params"]["tailnet"] = "none"
    cluster, approvals = world["cluster"], world["approvals"]
    del cluster.secrets[("tailscale-system", "operator-oauth")]  # no tailnet credential exists anywhere
    cluster.prepare_node()
    executor = world["executor"]()
    digest = executor.planner.plan(request, stack_revision=REV, target=cluster.identity(NODE),
                                   base_commit=world["repo"].head()).plan_digest
    approvals.approve("appr-iso", digest)
    run = executor.start(request=request, migration_plan_id="plan-demo", approval_id="appr-iso",
                         run_id="run-iso", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    assert run["status"] == "SUCCEEDED", {k: v for k, v in run["steps"].items() if v["status"] not in ("APPLIED", "SKIPPED")}
    assert {k for k, v in run["steps"].items() if v["status"] == "OMITTED"} == {
        "tailscale-oauth", "tailscale", "tailscale-sync", "tailscale-joined"}
    assert run["steps"]["verify"]["status"] == "APPLIED"
    assert not any(name.startswith("tailscale/") for name in world["repo"].read_tree(""))


def test_a_wif_target_runs_without_an_oauth_secret_and_waits_until_the_operator_logged_in(world):
    request = json.loads(json.dumps(REQUEST))
    request["params"].update({"tailnet": "wif", "tailnet_env": "target-gke",
                              "tailscale_client_id": "kFedTest123", "tailscale_audience": "api.tailscale.com/kFedTest123"})
    cluster, approvals = world["cluster"], world["approvals"]
    del cluster.secrets[("tailscale-system", "operator-oauth")]  # WIF: no OAuth secret exists anywhere
    cluster.prepare_node()
    executor = world["executor"]()
    digest = executor.planner.plan(request, stack_revision=REV, target=cluster.identity(NODE),
                                   base_commit=world["repo"].head()).plan_digest
    approvals.approve("appr-wif", digest)
    run = executor.start(request=request, migration_plan_id="plan-demo", approval_id="appr-wif",
                         run_id="run-wif", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    assert run["status"] == "SUCCEEDED", {k: v for k, v in run["steps"].items() if v["status"] not in ("APPLIED", "SKIPPED")}
    assert {k for k, v in run["steps"].items() if v["status"] == "OMITTED"} == {"tailscale-oauth"}
    assert run["steps"]["tailscale-joined"]["status"] == "APPLIED"


def test_synced_tailscale_is_not_a_join_until_the_operator_has_a_profile(world):
    request = json.loads(json.dumps(REQUEST))
    request["params"].update({"tailnet": "wif", "tailnet_env": "target-gke",
                              "tailscale_client_id": "kFedTest123", "tailscale_audience": "api.tailscale.com/kFedTest123"})
    cluster, approvals = world["cluster"], world["approvals"]
    del cluster.secrets[("tailscale-system", "operator-oauth")]
    cluster.secrets[("tailscale-system", "operator")] = ("Opaque", {"_machinekey": "x"})  # started, not logged in
    cluster.prepare_node()
    executor = world["executor"]()
    digest = executor.planner.plan(request, stack_revision=REV, target=cluster.identity(NODE),
                                   base_commit=world["repo"].head()).plan_digest
    approvals.approve("appr-wif2", digest)
    run = executor.start(request=request, migration_plan_id="plan-demo", approval_id="appr-wif2",
                         run_id="run-wif2", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    assert run["status"] != "SUCCEEDED"
    assert run["steps"]["tailscale-sync"]["status"] == "APPLIED"
    assert run["steps"]["tailscale-joined"]["status"] not in ("APPLIED", "SKIPPED")
    assert run["steps"]["verify"]["status"] not in ("APPLIED", "SKIPPED")


def test_a_sync_passes_progressing_only_where_the_stack_says_so(world):
    """OpenBao cannot be Ready before init and unseal; only its sync accepts Progressing."""

    class SealedCluster(FakeCluster):
        def application_status(self, name):
            status = super().application_status(name)
            sealed = name in (application_name("target-onprem", "openbao"), application_name("target-onprem", "base"))
            return status and (status | {"health": "Progressing"} if sealed else status)

    ticks = iter(range(0, 10**6, 100))
    cluster = SealedCluster()
    cluster.prepare_node()
    executor = world["executor"](cluster=cluster, monotonic=lambda: next(ticks))
    world["approvals"].approve("appr-1", executor.planner.plan(
        REQUEST, stack_revision=REV, target=cluster.identity(NODE), base_commit=world["repo"].head()).plan_digest)
    run = executor.start(request=REQUEST, migration_plan_id="plan-demo", approval_id="appr-1",
                         run_id="run-1", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    states = run["steps"]
    # base-sync declares nothing, so Progressing holds it; nothing after base can then start.
    assert (states["base-sync"]["status"], states["base-sync"]["reason"]) == ("HOLD", "sync_timeout")
    healthy_base = SealedCluster()
    healthy_base.application_status = lambda name: (FakeCluster.application_status(healthy_base, name) or None) and (
        FakeCluster.application_status(healthy_base, name) | (
            {"health": "Progressing"} if name == application_name("target-onprem", "openbao") else {}))
    healthy_base.prepare_node()
    executor = world["executor"](cluster=healthy_base, monotonic=lambda: next(ticks))
    world["approvals"].approve("appr-2", executor.planner.plan(
        REQUEST, stack_revision=REV, target=healthy_base.identity(NODE), base_commit=world["repo"].head()).plan_digest)
    run = executor.start(request=REQUEST, migration_plan_id="plan-demo", approval_id="appr-2",
                         run_id="run-2", started_by="applier", infra_plan_id=PLAN_ID, requested_by=REQUESTER)
    assert run["steps"]["openbao-sync"]["status"] == "APPLIED"  # declared: Healthy or Progressing


def test_the_join_check_waits_for_the_operator_login_instead_of_failing_the_first_read():
    """The operator writes its profile seconds after it is Ready (41: check_failed 7 s after login, then passed)."""
    reads, clock = [], [0.0]

    class LateLogin(FakeCluster):
        def secret_keys(self, namespace, name):
            reads.append(clock[0])
            return ["_machinekey"] if len(reads) < 4 else ["_current-profile", "_machinekey"]

    def sleep(seconds):
        clock[0] += seconds

    checks = ClusterChecks(LateLogin(), join_wait_s=120, sleep=sleep, monotonic=lambda: clock[0])
    run = {"plan": {"request": {"params": {}}, "target": {}}, "stack": {"steps": []}}
    assert checks.check("tailscale-joined", run) is True and len(reads) == 4
    reads.clear()
    never = ClusterChecks(FakeCluster(), join_wait_s=9, sleep=sleep, monotonic=lambda: clock[0])
    never.cluster.secrets[("tailscale-system", "operator")] = ("Opaque", {"_machinekey": "x"})
    assert never.check("tailscale-joined", run) is False  # bounded: it gives up after the window
