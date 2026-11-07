"""T44 executor over a real Git configuration repository and a simulated cluster (ADR-0032 3.4)."""

import json
import subprocess
import threading

import pytest
from test_plan import REPO, REQUEST, STACK, FakeCharts

from bravo_infra_plan import Planner, load_stack
from bravo_infra_plan.adapters import ClusterChecks, FileRunStore, GitConfigRepo
from bravo_infra_plan.applications import application_name
from bravo_infra_plan.executor import Executor

REV = "1" * 40
NODE = REQUEST["params"]["node_name"]


class FakeCluster:
    """Argo CD reports an Application synced at the revision it was given; resources become ready."""

    OPERATOR_SECRETS = {("tailscale-system", "operator-oauth"), ("target-dev", "target-seaweedfs-auth"),
                        ("bravo-secrets", "target-openbao-tls")}

    def __init__(self, *, never_ready=()):
        self.lock = threading.Lock()
        self.applied, self.helm, self.apps = [], {}, {}
        self.secrets = {key: ("Opaque", {"made": "by the operator"}) for key in self.OPERATOR_SECRETS}
        self.labelled = set()
        self.never_ready = set(never_ready)
        self.diff_calls = 0

    def identity(self, node_name):
        return {"cluster_uid": "cluster-1", "node_uid": f"uid-{node_name}"}

    def node_ready(self, node_name):
        return True

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

    def labelled_secret_exists(self, namespace, selector):
        return True

    def helm_digest(self, release, namespace):
        return self.helm.get((release, namespace))

    def helm_install(self, chart_dir, values_file, *, release, namespace, digest):
        assert chart_dir.is_dir() and values_file.is_file()
        self.helm[(release, namespace)] = digest


class FakeApprovals:
    def __init__(self):
        self.approvals, self.claims = {}, {}

    def approve(self, approval_id, digest):
        self.approvals[approval_id] = {"approval_id": approval_id, "binding": {"plan_digest": digest},
                                       "revoked": False, "claimed_run_id": None, "actor_id": "approver"}

    def read(self, plan_id, approval_id):
        return dict(self.approvals[approval_id], claimed_run_id=self.claims.get(approval_id))

    def claim(self, plan_id, approval_id, *, run_id, plan_digest):
        assert self.approvals[approval_id]["binding"]["plan_digest"] == plan_digest
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
        return Executor(planner, stack_revision=REV, cluster=cluster, repo=repo, approvals=approvals,
                        checks=ClusterChecks(cluster), store=FileRunStore(tmp_path / "runs"),
                        repo_url="file://config", ledger=ledger.append, sleep=lambda s: None, **kw)

    def approve(approval_id):
        digest = planner.plan(REQUEST, stack_revision=REV, target=cluster.identity(NODE),
                              base_commit=repo.head()).plan_digest
        approvals.approve(approval_id, digest)
        return digest

    return dict(executor=executor, approve=approve, cluster=cluster, approvals=approvals, repo=repo,
                remote=remote, ledger=ledger, tmp=tmp_path)


def start(world, run_id, approval_id="appr-1", **kw):
    return world["executor"](**kw).start(request=REQUEST, migration_plan_id="plan-demo", approval_id=approval_id,
                                         run_id=run_id, started_by="applier")


def test_a_plan_other_than_the_approved_digest_is_refused_before_anything_changes(world):
    world["approvals"].approve("appr-1", "f" * 64)
    head = world["repo"].head()
    run = start(world, "run-1")
    assert (run["status"], run["reason"]) == ("REFUSED", "plan_digest_mismatch")
    assert world["approvals"].claims == {} and world["repo"].head() == head
    assert world["cluster"].applied == [] and world["cluster"].helm == {}


def test_parallel_lines_run_until_human_steps_then_confirmations_finish_the_run(world):
    world["approve"]("appr-1")
    run = start(world, "run-1")
    states = {k: v["status"] for k, v in run["steps"].items()}
    assert run["status"] == "WAITING_OPERATOR"
    assert states["host-dirs"] == states["openbao-image"] == "WAITING_OPERATOR"  # code cannot observe these
    # Lines that do not need the host directories or the OpenBao image went ahead in parallel.
    for done in ("argocd", "base", "base-sync", "cnpg-sync", "eso-sync", "tailscale-sync", "db-secrets"):
        assert states[done] in ("APPLIED", "SKIPPED"), done
    for blocked in ("database", "objects", "openbao", "app", "verify"):
        assert states[blocked] == "PENDING", blocked

    executor = world["executor"]()
    executor.confirm("run-1", "host-dirs", confirmed_by="operator")
    run = executor.confirm("run-1", "openbao-image", confirmed_by="operator")
    assert run["status"] == "SUCCEEDED", {k: v for k, v in run["steps"].items() if v["status"] != "APPLIED"}
    assert all("started_at" in s and "ended_at" in s for s in run["steps"].values())

    # Every published path is in Git, and Argo CD points at the commit that published it.
    tree = world["repo"].read_tree("")
    assert {"base/accounts.json", "openbao/server.json", "app/application.json"} <= set(tree)
    assert "migration.json" not in {name.split("/")[-1] for name in tree}
    for path, commit in run["commits"].items():
        assert world["cluster"].apps[application_name("target-onprem", path)] == commit
    assert world["cluster"].helm[("target-gitops", "argocd")] == next(
        s["digest"] for s in run["plan"]["steps"] if s["id"] == "argocd")


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
    executor = world["executor"]()
    start(world, "run-1")
    executor.confirm("run-1", "host-dirs", confirmed_by="operator")
    executor.confirm("run-1", "openbao-image", confirmed_by="operator")
    head, applied, secrets = world["repo"].head(), len(world["cluster"].applied), dict(world["cluster"].secrets)

    world["approve"]("appr-2")  # a new plan: the base commit moved, so the digest is new
    run = start(world, "run-2", approval_id="appr-2")
    executor.confirm("run-2", "host-dirs", confirmed_by="operator")
    run = executor.confirm("run-2", "openbao-image", confirmed_by="operator")
    assert run["status"] == "SUCCEEDED"
    changed = {k: v["status"] for k, v in run["steps"].items() if v["status"] == "APPLIED"}
    # Only steps that re-check by observation (probes, confirmed human steps, waits, syncs) report APPLIED.
    assert {world_kind(k) for k in changed} <= {"probe", "operator_action", "gitops_sync", "wait"}, changed
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
                         run_id="run-1", started_by="applier")
    executor.confirm("run-1", "host-dirs", confirmed_by="operator")
    run = executor.confirm("run-1", "openbao-image", confirmed_by="operator")
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


def test_an_approval_already_claimed_by_another_run_is_refused(world):
    world["approve"]("appr-1")
    world["approvals"].claims["appr-1"] = "run-0"
    run = start(world, "run-1")
    assert (run["status"], run["reason"]) == ("REFUSED", "approval_unusable")
