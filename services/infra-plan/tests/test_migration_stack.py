"""target-onprem-migration: the destination's observable preparation as a migration target (T45 executor)."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_executor import FakeApprovals, FakeCluster, _git
from test_plan import REPO, FakeCharts

from bravo_infra_plan import Planner, load_stack, replication
from bravo_infra_plan.adapters import ClusterChecks, FileRunStore, GitConfigRepo, KubectlCluster
from bravo_infra_plan.executor import Executor, StepOutcome
from bravo_infra_plan.stack import StackError

STACK = REPO / "infra/stacks/target-onprem-migration/stack.json"
IAC = "a" * 40
REQUEST = {"stack": "target-onprem-migration", "environment_id": "env-onprem",
           "params": {"node_name": "bravo-target-1", "iac_revision": IAC}}
REV = "1" * 40


def declared_cluster():
    """target-db as T30's database render declares it: the external cluster's only owner."""
    done = subprocess.run([sys.executable, str(REPO / "infra/envs/target-workloads/render.py"), "database",
                           "--node-name", "bravo-target-1"], capture_output=True, text=True, check=True)
    return next(i for i in json.loads(done.stdout)["items"] if i["kind"] == "Cluster")


class MigrationCluster(FakeCluster):
    """A destination target-onprem just built: target-db declares t8-source; nothing else below is true yet."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.metadata_events, self.subscription, self.created = 0.0, None, []
        self.external = declared_cluster()["spec"]["externalClusters"]
        self.target, self.applied_status, self.copied, self.read_fails = "empty", False, False, False
        self.loaded = set()  # Secrets CNPG has loaded (they carry cnpg.io/reload)
        self.argo_blocked = None  # a reason T44's intake refuses with

    def prepared(self):
        self.metadata_events = 9.0
        self.apps["bravo-iac-target-dev"] = IAC
        self.secrets[("target-dev", "t8-source-replication")] = ("Opaque", {"password": "x"})
        self.secrets[("target-dev", "t8-source-ca")] = ("Opaque", {"ca.crt": "x"})
        self.loaded = {"t8-source-replication", "t8-source-ca"}
        return self

    def secret_keys(self, namespace, name):
        found = self.secrets.get((namespace, name))
        return None if found is None else sorted(found[1])

    def argo_intake(self, repo_root, environment, cluster_uid, revision):
        assert environment == "target-dev" and cluster_uid == self.identity("bravo-target-1")["cluster_uid"]
        if self.argo_blocked:
            return {"status": "BLOCKED", "reasons": [self.argo_blocked]}
        live = self.apps.get("bravo-iac-target-dev")
        return {"status": "NOT_INSTALLED" if live is None else "SYNC_VERIFIED" if live == revision else "SYNC_PENDING"}

    def audit_level_events(self, level):
        return self.metadata_events if level == "Metadata" else 0.0

    def get_object(self, item):
        if item["kind"] == "Cluster":
            versions = {name: ("7" if name in self.loaded else "") for name in ("t8-source-replication", "t8-source-ca")}
            return None if self.external is None else {"spec": {"externalClusters": self.external}, "status": {
                "secretsResourceVersion": {"externalClusterSecretVersion": versions}}}
        if item["kind"] == "Subscription" and self.subscription is not None:
            return {**self.subscription, "status": {"applied": self.applied_status}}
        return None

    def create(self, items):
        assert self.subscription is None
        self.created.extend(items)
        self.subscription = json.loads(json.dumps(items[0]))

    def primary_query(self, namespace, cluster, database, query):
        assert (namespace, cluster, database) == ("target-dev", "target-db", "chat")
        if self.read_fails and query == replication.COPIED:
            raise RuntimeError("primary_read_failed")
        return {replication.PRESENT: [["1" if self.subscription else "0", "2"]],
                replication.FILLED: [["t" if self.target == "rows" else "f"]],
                replication.COPIED: [["2", "0" if self.copied else "1"]]}[query]


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
    cluster, approvals = MigrationCluster(), FakeApprovals()
    stack = load_stack(STACK)
    planner = Planner(stack, REPO, chart_renderer=FakeCharts())
    repo = GitConfigRepo(str(remote), "main", tmp_path / "work")

    def executor(cluster=cluster):
        ticks = iter(range(0, 10**9, 5))
        return Executor(planner, stack_revision=REV, cluster=cluster, repo=repo, approvals=approvals,
                        checks=ClusterChecks(cluster), store=FileRunStore(tmp_path / "runs"),
                        repo_url="file://config", ledger=lambda record: None, sleep=lambda s: None,
                        monotonic=lambda: next(ticks))

    def approve(approval_id, request=REQUEST):
        digest = planner.plan(request, stack_revision=REV, target=cluster.identity("bravo-target-1"),
                              base_commit=repo.head()).plan_digest
        approvals.approve(approval_id, digest)
        return digest

    return dict(executor=executor, approve=approve, cluster=cluster, planner=planner, repo=repo)


def start(world, run_id, approval_id="appr-1", cluster=None):
    executor = world["executor"]() if cluster is None else world["executor"](cluster)
    return executor.start(request=REQUEST, migration_plan_id="plan-demo", approval_id=approval_id,
                          run_id=run_id, started_by="applier")


def states(run):
    return {k: v["status"] for k, v in run["steps"].items()}


def test_the_stack_holds_only_steps_the_destination_can_observe():
    stack = json.loads(STACK.read_text())
    steps = {s["id"]: s for s in stack["steps"]}
    assert list(steps) == ["identity", "node-files", "iac-network", "replication-external-cluster",
                           "replication-credentials", "replication-target-ready", "forward-subscription",
                           "initial-copy"]
    # The write waits for the route to the source and for the audit policy that records it.
    assert steps["forward-subscription"]["needs"] == ["replication-target-ready", "iac-network", "node-files"]
    assert steps["forward-subscription"]["create_only"] is True
    assert steps["forward-subscription"]["renders"][0]["args"] == ["subscription", "--side", "target"]
    assert steps["replication-target-ready"]["needs"] == ["replication-credentials"]
    assert steps["replication-credentials"]["needs"] == ["replication-external-cluster"]
    assert "cnpg.io/reload=true" in steps["replication-credentials"]["instruction"]
    # The only cluster write makes the forward subscription; nothing prunes, deletes or drops.
    assert [s["id"] for s in stack["steps"] if s["kind"] not in ("probe", "operator_action")] == ["forward-subscription"]
    for word in ("delete", "prune", "DROP"):
        assert word not in STACK.read_text()


def test_the_plan_renders_the_reviewed_forward_subscription_and_binds_the_argo_revision(world):
    def plan(request=REQUEST):
        return world["planner"].plan(request, stack_revision=REV, target={"cluster_uid": "c", "node_uid": "n"},
                                     base_commit="2" * 40)

    first, again = plan(), plan()
    assert first.plan_digest == again.plan_digest
    [text] = first.files["forward-subscription"].values()
    [item] = json.loads(text)["items"]
    assert item["kind"] == "Subscription" and item["metadata"]["name"] == "t8-source-to-target"
    params = item["spec"]["parameters"]
    assert params["enabled"] == params["copy_data"] == "true" and params["origin"] == "none"
    assert item["spec"]["externalClusterName"] == "t8-source" and item["spec"]["publicationName"] == "source_chat"
    other = {**REQUEST, "params": {**REQUEST["params"], "iac_revision": "b" * 40}}
    assert plan(other).plan_digest != first.plan_digest


def test_a_fresh_destination_waits_for_each_condition_then_creates_the_subscription_once(world):
    world["approve"]("appr-1")
    s = states(start(world, "run-1"))
    assert s["identity"] == "APPLIED"
    for waiting in ("node-files", "iac-network", "replication-credentials"):
        assert s[waiting] == "WAITING_OPERATOR", waiting
    assert s["forward-subscription"] == "PENDING"

    cluster = world["cluster"].prepared()
    executor = world["executor"]()
    s = states(executor.resume("run-1"))
    assert s["replication-target-ready"] == "APPLIED"
    assert s["forward-subscription"] == "APPLIED" and s["initial-copy"] == "WAITING_OPERATOR"
    assert [i["kind"] for i in cluster.created] == ["Subscription"] and cluster.applied == []  # create, not apply

    cluster.applied_status = True
    assert states(executor.resume("run-1"))["initial-copy"] == "WAITING_OPERATOR"  # applied, still copying
    cluster.copied = True
    run = executor.resume("run-1")
    assert run["status"] == "SUCCEEDED", states(run)


def test_a_destination_built_before_the_declaration_fails_before_any_write(world):
    cluster = world["cluster"].prepared()
    cluster.external = None  # an older database render, or someone else's patch removed with it
    world["approve"]("appr-1")
    s = states(start(world, "run-1"))
    assert s["replication-external-cluster"] == "FAILED"
    assert s["replication-credentials"] == s["forward-subscription"] == "PENDING" and cluster.created == []


def test_a_destination_already_prepared_writes_nothing(world):
    cluster = world["cluster"].prepared()
    rendered = json.loads(next(iter(world["planner"].plan(
        REQUEST, stack_revision=REV, target=cluster.identity("bravo-target-1"),
        base_commit=world["repo"].head()).files["forward-subscription"].values())))
    live = rendered["items"][0] if "items" in rendered else rendered
    cluster.subscription = live
    cluster.applied_status = cluster.copied = True
    world["approve"]("appr-1")
    run = start(world, "run-1")
    assert run["status"] == "SUCCEEDED", states(run)
    assert states(run)["forward-subscription"] == "SKIPPED" and cluster.created == []


def test_a_tuned_live_subscription_is_never_taken_over(world):
    cluster = world["cluster"].prepared()
    cluster.subscription = {"kind": "Subscription", "spec": {"name": "t8_source_to_target", "parameters": {
        "enabled": "true", "copy_data": "true", "disable_on_error": "true"}}}  # tuned by the T8 runbook
    world["approve"]("appr-1")
    s = states(start(world, "run-1"))
    assert s["forward-subscription"] == "HOLD" and cluster.created == []


def test_a_target_that_already_has_rows_holds_before_any_copy(world):
    cluster = world["cluster"].prepared()
    cluster.target = "rows"
    world["approve"]("appr-1")
    run = start(world, "run-1")
    s = states(run)
    assert s["replication-target-ready"] == "HOLD"
    assert run["steps"]["replication-target-ready"]["reason"] == "replication_target_not_empty"  # not adapter_error
    assert s["forward-subscription"] == "PENDING" and cluster.created == []


def test_a_failed_read_holds_even_when_a_person_confirmed(world):
    cluster = world["cluster"].prepared()
    cluster.applied_status, cluster.read_fails = True, True
    world["approve"]("appr-1")
    start(world, "run-1")
    run = world["executor"]().confirm("run-1", "initial-copy", confirmed_by="operator")
    assert states(run)["initial-copy"] == "HOLD" and run["status"] != "SUCCEEDED"


def test_another_argo_revision_is_not_the_approved_network(world):
    cluster = world["cluster"].prepared()
    cluster.apps["bravo-iac-target-dev"] = "c" * 40
    world["approve"]("appr-1")
    assert states(start(world, "run-1"))["iac-network"] == "WAITING_OPERATOR"


def test_a_switched_cluster_holds_before_any_destination_step(world):
    world["approve"]("appr-1")

    class Other(MigrationCluster):
        def identity(self, node_name):
            return {"cluster_uid": "another-cluster", "node_uid": f"uid-{node_name}"}

    run = start(world, "run-1", cluster=Other())
    assert run["status"] != "SUCCEEDED"
    assert all(v["status"] in ("PENDING", "HOLD", "REFUSED") for v in run["steps"].values()), states(run)


def test_the_external_cluster_is_declared_by_the_database_render_and_only_checked_here():
    # The stack never writes target-db: the first build's database render owns it (one owner under selfHeal).
    stack = {s["id"]: s for s in json.loads(STACK.read_text())["steps"]}
    assert stack["replication-external-cluster"]["kind"] == "probe"
    assert "external-patch" not in STACK.read_text()
    # One declaration: the check compares against the same dict the render writes.
    assert declared_cluster()["spec"]["externalClusters"] == [replication.DECLARATION]
    assert "hostaddr" not in replication.DECLARATION["connectionParameters"]  # kube DNS, not an observed IP
    assert declared_cluster()["spec"]["postgresql"]["parameters"]["max_slot_wal_keep_size"] == "1GB"


def test_the_checks_look_at_keys_tls_and_the_route_not_only_names():
    cluster = MigrationCluster().prepared()
    run = {"plan": {"request": REQUEST, "target": cluster.identity("bravo-target-1")}}
    checks = ClusterChecks(cluster)
    assert checks.check("replication-credentials", run) and checks.check("replication-external-cluster", run)
    cluster.loaded = {"t8-source-replication"}  # made after the Cluster without cnpg.io/reload: never loaded
    assert checks.check("replication-credentials", run) is False
    cluster.loaded = {"t8-source-replication", "t8-source-ca"}
    cluster.secrets[("target-dev", "t8-source-ca")] = ("Opaque", {"tls.crt": "x"})
    assert checks.check("replication-credentials", run) is False
    [declared] = cluster.external
    for changed in ({"sslmode": "require"}, {"hostaddr": "10.43.0.9"}, {"host": "source-db-rw.source-dev.svc"}):
        params = {**declared["connectionParameters"], **changed}
        cluster.external = [{**declared, "connectionParameters": params}]
        assert checks.check("replication-external-cluster", run) is False, changed
    cluster.external = [declared, declared]  # two entries of one name: not a single declaration
    assert checks.check("replication-external-cluster", run) is False

    assert checks.check("iac-network-synced", run) is True
    cluster.apps.pop("bravo-iac-target-dev")
    assert checks.check("iac-network-synced", run) is False  # no Argo yet: wait, do not fail the run
    cluster.argo_blocked = "APPLICATION_TARGET_MISMATCH"
    with pytest.raises(StepOutcome, match="argo_application_target_mismatch"):
        checks.check("iac-network-synced", run)


def test_the_argo_check_runs_t44s_intake_itself():
    cluster = KubectlCluster.__new__(KubectlCluster)
    cluster.kubeconfig, cluster.context, cluster.kubectl = "/nonexistent/kubeconfig", None, "kubectl"
    result = cluster.argo_intake(REPO, "target-dev", "uid-1", IAC)
    assert result["status"] == "BLOCKED" and result["reasons"] == ["EXPLICIT_KUBECONFIG_REQUIRED"]
    run = {"plan": {"request": REQUEST, "target": {"cluster_uid": "uid-1"}}}
    with pytest.raises(StepOutcome, match="argo_explicit_kubeconfig_required"):
        ClusterChecks(cluster, REPO).check("iac-network-synced", run)


class Kubectl:
    """KubectlCluster with kubectl replaced by canned results, to pin parsing and the fixed queries."""

    def __init__(self, results):
        self.calls, self.results = [], list(results)
        self.cluster = KubectlCluster.__new__(KubectlCluster)
        self.cluster._k = self._k

    def _k(self, *args, stdin=None, check=True):
        self.calls.append(args)
        code, out = self.results.pop(0)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


PRIMARY = json.dumps({"items": [{"metadata": {"name": "target-db-1"}}]})


def test_audit_events_are_read_from_the_api_server_metrics():
    metrics = 'apiserver_audit_level_total{level="Metadata"} 7\napiserver_audit_level_total{level="None"} 2\n'
    k = Kubectl([(0, metrics), (0, 'apiserver_audit_level_total{level="None"} 2\n')])
    assert k.cluster.audit_level_events("Metadata") == 7.0
    assert k.cluster.audit_level_events("Metadata") is None  # a None-only policy never counts Metadata
    with pytest.raises(StackError):
        k.cluster.audit_level_events("Everything")


SUBSCRIPTION = (0, json.dumps({"kind": "Subscription", "status": {"applied": True}}))


@pytest.mark.parametrize("answer, expected", [("3|0", True), ("3|1", False), ("0|0", False)])
def test_the_initial_copy_is_a_fixed_query_in_a_read_only_transaction(answer, expected):
    k = Kubectl([SUBSCRIPTION, (0, PRIMARY), (0, answer + "\n")])
    assert replication.initial_copy(k.cluster) is expected
    assert k.calls[0][:3] == ("get", "subscription.postgresql.cnpg.io", "t8-source-to-target")
    call = k.calls[2]
    assert call[:6] == ("exec", "-n", "target-dev", "target-db-1", "-c", "postgres")
    assert call[-6:-4] == ("-c", "BEGIN READ ONLY") and call[-2:] == ("-c", "ROLLBACK")
    assert "ON_ERROR_STOP=1" in call  # a failed middle query fails psql, not only the last command
    assert call[-3] == replication.COPIED and "subname = 't8_source_to_target'" in call[-3]


def test_the_queries_are_fixed_reads():
    for query in (replication.COPIED, replication.PRESENT, replication.FILLED):
        assert query.startswith("SELECT ")
        assert not any(word in query.upper() for word in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", ";"))


def test_no_single_primary_waits_and_a_failed_read_holds():
    none = json.dumps({"items": []})
    assert replication.initial_copy(Kubectl([SUBSCRIPTION, (0, none)]).cluster) is False
    assert replication.initial_copy(Kubectl([(0, json.dumps({"status": {}}))]).cluster) is False  # not applied
    with pytest.raises(RuntimeError, match="primary_read_failed"):
        replication.initial_copy(Kubectl([SUBSCRIPTION, (0, PRIMARY), (1, "")]).cluster)
    with pytest.raises(StackError):
        Kubectl([]).cluster.primary_query("target-dev", "target-db", "chat; DROP", "SELECT 1")


@pytest.mark.parametrize("message, reason", [
    ('could not connect to the publisher: server certificate for "source-db-rw" does not match host name '
     '"source-db-for-replication.target-dev.svc"', "source_tls_name_mismatch"),
    ('root certificate file "/controller/external/t8-source/t8-source-ca_ca.crt" does not exist', "source_ca_not_loaded"),
])
def test_a_subscription_that_cannot_connect_for_good_holds_with_the_reason(message, reason):
    stuck = (0, json.dumps({"status": {"applied": False, "message": "while creating subscription: ERROR: " + message}}))
    with pytest.raises(StepOutcome, match=reason):
        replication.initial_copy(Kubectl([stuck]).cluster)
    retrying = (0, json.dumps({"status": {"applied": False, "message": "connection timed out"}}))
    assert replication.initial_copy(Kubectl([retrying]).cluster) is False  # may heal: keep waiting


@pytest.mark.parametrize("answers, result", [
    ([(0, PRIMARY), (0, "1|2\n")], True),                                # the subscription is already there
    ([(0, PRIMARY), (0, "0|2\n"), (0, PRIMARY), (0, "f\n")], True),     # tables exist and are empty
    ([(0, PRIMARY), (0, "0|2\n"), (0, PRIMARY), (0, "t\n")], "replication_target_not_empty"),
    ([(0, PRIMARY), (0, "0|1\n")], "replication_target_schema_missing"),
    ([(0, json.dumps({"items": []}))], "primary_unavailable"),
])
def test_copy_data_starts_only_on_an_empty_target_with_its_schema(answers, result):
    k = Kubectl(answers)
    if result is True:
        assert replication.target_ready(k.cluster) is True
    else:
        with pytest.raises(StepOutcome, match=result):
            replication.target_ready(k.cluster)
