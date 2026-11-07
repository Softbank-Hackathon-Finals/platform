"""target-onprem-migration: the destination's own steps as a migration target (T45 stack, T44 node files)."""

import json

import pytest
from test_executor import FakeApprovals, FakeCluster, _git
from test_plan import REPO, FakeCharts

from bravo_infra_plan import Planner, load_stack
from bravo_infra_plan.adapters import ClusterChecks, FileRunStore, GitConfigRepo
from bravo_infra_plan.executor import Executor

STACK = REPO / "infra/stacks/target-onprem-migration/stack.json"
REQUEST = {"stack": "target-onprem-migration", "environment_id": "env-onprem", "params": {"node_name": "bravo-target-1"}}
REV = "1" * 40
HUMAN_ONLY = ("reverse-prepared", "platform-handover")  # code cannot observe these from the destination


class MigrationCluster(FakeCluster):
    """A fresh destination: no audit events, no IaC sync, no replication until the test makes them true."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.metadata_events, self.external, self.subscription, self.copied = 0.0, False, False, False

    def audit_level_events(self, level):
        return self.metadata_events if level == "Metadata" else 0.0

    def external_cluster(self, namespace, cluster, external):
        return self.external and (namespace, cluster, external) == ("target-dev", "target-db", "t8-source")

    def subscription_applied(self, namespace, name):
        return self.subscription and any('"t8-source-to-target"' in item for item in self.applied)

    def subscription_copied(self, namespace, cluster, database, subscription):
        return self.copied


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

    def approve(approval_id):
        digest = planner.plan(REQUEST, stack_revision=REV, target=cluster.identity("bravo-target-1"),
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


def test_the_stack_orders_node_files_and_network_before_replication_and_never_deletes():
    stack = json.loads(STACK.read_text())
    steps = {s["id"]: s for s in stack["steps"]}
    assert steps["forward-subscription"]["needs"] == ["replication-external-cluster", "node-files"]
    assert "iac-network" in steps["replication-external-cluster"]["needs"]
    assert steps["platform-handover"]["needs"] == ["reverse-prepared", "iac-network"]
    # Making is the only cluster write: the forward subscription; nothing prunes, disables or deletes.
    assert [s["id"] for s in stack["steps"] if s["kind"] not in ("probe", "operator_action")] == ["forward-subscription"]
    text = STACK.read_text()
    for word in ("delete", "prune", "DROP", "disable"):
        assert word not in text.replace("disable_on_error", "")


def test_the_plan_renders_the_reviewed_forward_subscription_and_is_deterministic(world):
    first = world["planner"].plan(REQUEST, stack_revision=REV, target={"cluster_uid": "c", "node_uid": "n"},
                                  base_commit="2" * 40)
    again = world["planner"].plan(REQUEST, stack_revision=REV, target={"cluster_uid": "c", "node_uid": "n"},
                                  base_commit="2" * 40)
    assert first.plan_digest == again.plan_digest
    [text] = first.files["forward-subscription"].values()
    [item] = json.loads(text)["items"]
    assert item["kind"] == "Subscription" and item["metadata"] == {
        "name": "t8-source-to-target", "namespace": "target-dev",
        "labels": {"component": "target", "app.kubernetes.io/part-of": "bravo-t8-replication"}}
    params = item["spec"]["parameters"]
    assert params["enabled"] == params["copy_data"] == "true" and params["origin"] == "none"
    assert item["spec"]["externalClusterName"] == "t8-source" and item["spec"]["publicationName"] == "source_chat"
    moved = world["planner"].plan(REQUEST, stack_revision="3" * 40, target={"cluster_uid": "c", "node_uid": "n"},
                                  base_commit="2" * 40)
    assert moved.plan_digest != first.plan_digest


def test_a_fresh_destination_waits_for_each_condition_then_hands_over(world):
    world["approve"]("appr-1")
    run = start(world, "run-1")
    s = states(run)
    assert s["identity"] == "APPLIED"
    for waiting in ("node-files", "iac-network", "replication-credentials"):
        assert s[waiting] == "WAITING_OPERATOR", waiting
    assert s["forward-subscription"] == "PENDING" and s["platform-handover"] == "PENDING"

    # The operator applies the node bundle, the destination Argo CD syncs, the credentials are delivered.
    cluster = world["cluster"]
    cluster.metadata_events = 1200.0
    cluster.apps["bravo-iac-target-dev"] = "r" * 40
    cluster.secrets[("target-dev", "t8-source-replication")] = ("Opaque", {"password": "x"})
    cluster.secrets[("target-dev", "t8-source-ca")] = ("Opaque", {"ca.crt": "x"})
    executor = world["executor"]()
    s = states(executor.resume("run-1"))
    assert s["node-files"] == s["iac-network"] == s["replication-credentials"] == "SKIPPED"  # observed, not confirmed
    assert s["replication-external-cluster"] == "WAITING_OPERATOR"

    cluster.external = True
    s = states(executor.resume("run-1"))
    assert s["forward-subscription"] == "APPLIED" and s["initial-copy"] == "WAITING_OPERATOR"
    assert any('"t8-source-to-target"' in item for item in cluster.applied)

    cluster.subscription = True
    assert states(executor.resume("run-1"))["initial-copy"] == "WAITING_OPERATOR"  # applied, still copying
    cluster.copied = True
    s = states(executor.resume("run-1"))
    assert s["initial-copy"] == "SKIPPED" and s["reverse-prepared"] == "WAITING_OPERATOR"

    for step in HUMAN_ONLY:
        run = executor.confirm("run-1", step, confirmed_by="operator")
    assert run["status"] == "SUCCEEDED", states(run)
    assert [json.loads(i)["kind"] for i in cluster.applied] == ["Subscription"]  # the only write


def test_a_destination_already_prepared_changes_nothing(world):
    cluster = world["cluster"]
    cluster.metadata_events, cluster.external, cluster.subscription, cluster.copied = 5.0, True, True, True
    cluster.apps["bravo-iac-target-dev"] = "r" * 40
    cluster.secrets[("target-dev", "t8-source-replication")] = ("Opaque", {"password": "x"})
    cluster.secrets[("target-dev", "t8-source-ca")] = ("Opaque", {"ca.crt": "x"})
    world["approve"]("appr-1")
    run = start(world, "run-1")
    assert states(run)["forward-subscription"] == "APPLIED"
    before = list(cluster.applied)
    executor = world["executor"]()
    for step in HUMAN_ONLY:
        run = executor.confirm("run-1", step, confirmed_by="operator")
    assert run["status"] == "SUCCEEDED"

    world["approve"]("appr-2")
    run = start(world, "run-2", approval_id="appr-2")
    assert states(run)["forward-subscription"] == "SKIPPED"  # the subscription is already there
    assert cluster.applied == before


def test_a_switched_cluster_holds_before_any_destination_step(world):
    world["approve"]("appr-1")

    class Other(MigrationCluster):
        def identity(self, node_name):
            return {"cluster_uid": "another-cluster", "node_uid": f"uid-{node_name}"}

    run = start(world, "run-1", cluster=Other())
    assert run["status"] != "SUCCEEDED"
    assert all(v["status"] in ("PENDING", "HOLD", "REFUSED") for v in run["steps"].values()), states(run)


def test_the_checks_name_only_the_reviewed_objects():
    cluster = MigrationCluster()
    checks = ClusterChecks(cluster)
    run = {"plan": {"request": REQUEST, "target": cluster.identity("bravo-target-1")}}
    assert checks.check("audit-metadata-active", run) is False
    cluster.metadata_events = 1.0
    assert checks.check("audit-metadata-active", run) is True
    assert checks.check("reverse-prepared", run) is None and checks.check("platform-handover", run) is None
    assert checks.check("initial-copy-complete", run) is False

    class NoArgo(MigrationCluster):
        def application_status(self, name):
            raise RuntimeError("kubectl get failed")  # no applications.argoproj.io on this cluster yet

    assert ClusterChecks(NoArgo()).check("iac-network-synced", run) is False  # wait, do not fail the run


class Kubectl:
    """KubectlCluster with kubectl replaced by canned results, to pin parsing and the fixed query."""

    def __init__(self, results):
        from types import SimpleNamespace

        from bravo_infra_plan.adapters import KubectlCluster
        self.calls, self.results, self.ns = [], list(results), SimpleNamespace
        self.cluster = KubectlCluster.__new__(KubectlCluster)
        self.cluster._k = self._k

    def _k(self, *args, stdin=None, check=True):
        self.calls.append(args)
        code, out = self.results.pop(0)
        return self.ns(returncode=code, stdout=out, stderr="")


def test_audit_events_are_read_from_the_api_server_metrics():
    from bravo_infra_plan.stack import StackError
    metrics = ('apiserver_audit_event_total 9\napiserver_audit_level_total{level="Metadata"} 7\n'
               'apiserver_audit_level_total{level="None"} 2\n')
    k = Kubectl([(0, metrics), (0, 'apiserver_audit_level_total{level="None"} 2\n')])
    assert k.cluster.audit_level_events("Metadata") == 7.0
    assert k.cluster.audit_level_events("Metadata") is None  # a None-only policy never counts Metadata
    assert k.calls[0] == ("get", "--raw", "/metrics")
    with pytest.raises(StackError):
        k.cluster.audit_level_events("Everything")


@pytest.mark.parametrize("answer, expected", [("3|0", True), ("3|1", False), ("0|0", False)])
def test_the_initial_copy_is_judged_by_a_fixed_read_only_query(answer, expected):
    primary = json.dumps({"items": [{"metadata": {"name": "target-db-1"}}]})
    k = Kubectl([(0, primary), (0, answer + "\n")])
    assert k.cluster.subscription_copied("target-dev", "target-db", "chat", "t8_source_to_target") is expected
    exec_call = k.calls[1]
    assert exec_call[:6] == ("exec", "-n", "target-dev", "target-db-1", "-c", "postgres")
    query = exec_call[-1]
    assert query.startswith("SELECT count(*)") and "subname = 't8_source_to_target'" in query
    assert not any(word in query.upper() for word in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))


def test_an_unreadable_primary_is_unobservable_not_done():
    from bravo_infra_plan.stack import StackError
    none = json.dumps({"items": []})
    two = json.dumps({"items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]})
    one = json.dumps({"items": [{"metadata": {"name": "a"}}]})
    assert Kubectl([(0, none)]).cluster.subscription_copied("target-dev", "target-db", "chat", "s") is None
    assert Kubectl([(0, two)]).cluster.subscription_copied("target-dev", "target-db", "chat", "s") is None
    assert Kubectl([(0, one), (1, "")]).cluster.subscription_copied("target-dev", "target-db", "chat", "s") is None
    with pytest.raises(StackError):
        Kubectl([]).cluster.subscription_copied("target-dev", "target-db", "chat", "x'; DROP TABLE t; --")
