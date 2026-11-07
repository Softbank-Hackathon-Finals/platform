"""A10 cluster stacks (cluster-gke, cluster-eks) and the kind-generalised executor ports (ADR-0034).

Offline: plans are rendered with the real repository renderers and a stand-in for `helm template`; no
cloud, cluster or Git remote is contacted. The end-to-end run uses the real terraform binary with a
local-backend fixture standing in for the cluster root.
"""

import copy
import json
import re
import shutil

import pytest
from test_executor import FakeApprovals, FakeCluster
from test_plan import REPO, FakeCharts

from bravo_infra_plan import Planner, StackError, load_stack, parse_stack
from bravo_infra_plan import adapters as adapters_module
from bravo_infra_plan.adapters import (
    AwsIdentity,
    CachedOutputs,
    ClusterChecks,
    ClusterIdentity,
    FileRunStore,
    GcpIdentity,
    identity_for,
)
from bravo_infra_plan.executor import Executor
from bravo_infra_plan.stack import environment_of

STACKS = {"cluster-gke": {"kind": "gcp", "extra": {"project_id": "bravo-demo-project"}, "target": {"project_id": "bravo-demo-project"},
                          "foundation": "gcp-foundation", "storage": "bravo-pd-balanced"},
          "cluster-eks": {"kind": "aws", "extra": {"region": "ap-northeast-2"}, "target": {"account_id": "000000000000"},
                          "foundation": "aws-foundation", "storage": "bravo-gp3"}}
REV, BASE = "1" * 40, "2" * 40


def _request(name, side, private):
    return {"stack": name, "environment_id": f"env-{side}",
            "params": {"private_dir": str(private), "side": side, "kube_version": "v1.35.2", **STACKS[name]["extra"]}}


def _private(tmp_path, stack):
    for step in stack.document["steps"]:
        if step["kind"] == "terraform" and not (tmp_path / f'{step["id"]}.backend.hcl').exists():
            (tmp_path / f'{step["id"]}.backend.hcl').write_text("# private\n")
            (tmp_path / f'{step["id"]}.tfvars.json').write_text("{}")
    return tmp_path


def _plan(name, side, tmp_path):
    stack = load_stack(REPO / f"infra/stacks/{name}/stack.json")
    request = _request(name, side, _private(tmp_path, stack))
    return stack, Planner(stack, REPO, chart_renderer=FakeCharts()).plan(
        request, stack_revision=REV, target=STACKS[name]["target"], base_commit=BASE)


@pytest.mark.parametrize("name", sorted(STACKS))
@pytest.mark.parametrize("side", ["source", "target"])
def test_cluster_stack_plans_offline_for_either_side(name, side, tmp_path):
    stack, first = _plan(name, side, tmp_path)
    _, second = _plan(name, side, tmp_path)
    assert first.plan_digest == second.plan_digest and first.files == second.files
    assert stack.kind == STACKS[name]["kind"] and stack.terraform_steps and stack.cluster_steps
    steps = {s["id"]: s for s in first.document["steps"]}
    assert steps[STACKS[name]["foundation"]]["change_set"] == "UNPLANNED"  # offline: no terraform client
    assert first.document["levels"][:3] == [[STACKS[name]["foundation"]], ["cluster"], ["cluster-access"]]
    items = [i for files in first.files.values() for text in files.values() for i in json.loads(text)["items"]]
    app = f"{side}-dev"
    assert {i["metadata"]["name"] for i in items if i["kind"] == "Namespace"} >= {app, "external-secrets",
                                                                                  "tailscale-system"}
    cluster = next(i for i in items if i["kind"] == "Cluster")
    assert (cluster["metadata"]["namespace"], cluster["metadata"]["name"]) == (app, f"{side}-db")
    assert cluster["spec"]["storage"]["storageClass"] == STACKS[name]["storage"]
    # The disks the CSI driver creates carry the component label/tag too (A11: `standard-rwo` PDs had none).
    volume_class = next(i for i in items if i["kind"] == "StorageClass")
    assert volume_class["metadata"]["name"] == STACKS[name]["storage"]
    assert f"component={side}" in volume_class["parameters"].values()
    assert {r["passwordSecret"]["name"] for r in cluster["spec"]["managed"]["roles"]} == {
        f"{side}-app-db", f"{side}-replication"}
    other = "target" if side == "source" else "source"
    assert f'"{other}-dev"' not in json.dumps(items) and f'"{other}-db"' not in json.dumps(items)
    store = next(i for i in items if i["kind"] == "SecretStore")
    assert store["metadata"]["namespace"] == app
    provider = store["spec"]["provider"]
    assert ("gcpsm" in provider) == (name == "cluster-gke") and ("aws" in provider) == (name == "cluster-eks")
    assert "auth" not in json.dumps(provider)  # workload identity of the ESO controller, no key reference
    edge = next(i for i in items if i["kind"] == "Deployment" and i["metadata"]["name"] == "bravo-edge-tunnel")
    assert edge["metadata"]["namespace"] == app
    for item in items:  # component label on everything this stack renders itself (charts are stand-ins here)
        if item["kind"] in ("Namespace", "Cluster", "SecretStore", "StorageClass", "Deployment") \
                and item["metadata"]["name"] != "cnpg-system":  # the planner's chart namespace (T45)
            assert "component" in item["metadata"]["labels"], item["kind"]


@pytest.mark.parametrize("name", sorted(STACKS))
def test_cluster_stack_terraform_wiring_names_declared_variables_and_binds_private_inputs(name, tmp_path):
    stack = load_stack(REPO / f"infra/stacks/{name}/stack.json")
    for step in stack.document["steps"]:
        if step["kind"] != "terraform":
            continue
        declared = set(re.findall(r'^variable "([^"]+)"', "".join(
            p.read_text() for p in (REPO / step["dir"]).glob("*.tf")), re.M))
        assert set(step.get("vars", {})) <= declared, step["id"]
    _, plan = _plan(name, "target", tmp_path)
    (tmp_path / "cluster.tfvars.json").write_text('{"node_count": 2}')
    _, changed = _plan(name, "target", tmp_path)
    assert changed.plan_digest != plan.plan_digest
    assert "node_count" not in json.dumps(changed.document)  # bound by digest only


def test_cluster_stack_side_is_a_closed_choice(tmp_path):
    stack = load_stack(REPO / "infra/stacks/cluster-gke/stack.json")
    request = _request("cluster-gke", "target", _private(tmp_path, stack))
    request["params"]["side"] = "prod"
    with pytest.raises(StackError, match="invalid_plan_request"):
        Planner(stack, REPO, chart_renderer=FakeCharts()).plan(request, stack_revision=REV,
                                                               target={"project_id": "x"}, base_commit=BASE)


def test_existing_stacks_keep_their_kinds():
    onprem = load_stack(REPO / "infra/stacks/target-onprem/stack.json")
    assert onprem.kind == "onprem" and not onprem.terraform_steps
    assert environment_of(onprem.document)["secret_store"] == {"kind": "Pod", "namespace": "bravo-secrets",
                                                               "name": "target-openbao-0"}
    platform = load_stack(REPO / "infra/stacks/aws-platform/stack.json")
    assert platform.kind == "aws" and not platform.cluster_steps


def test_environment_references_must_be_declared():
    document = json.loads((REPO / "infra/stacks/cluster-gke/stack.json").read_text())
    bad = copy.deepcopy(document)
    bad["environment"]["database"] = "${missing}"
    with pytest.raises(StackError, match="unknown_parameter"):
        parse_stack(bad)
    bad = copy.deepcopy(document)
    bad["environment"]["cluster_endpoint"]["from"] = "argocd"
    with pytest.raises(StackError, match="cluster_endpoint_without_terraform"):
        parse_stack(bad)


# --- identity registry (both.identity) ----------------------------------------------------------------


class _Result:
    def __init__(self, stdout):
        self.stdout, self.returncode, self.stderr = stdout, 0, ""


def test_identity_registry_by_kind(monkeypatch):
    calls = []

    def fake_run(command, **kw):
        calls.append(command[1:])
        if command[0] == "aws":
            return _Result('{"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/x/y"}')
        if command[1:3] == ["config", "get-value"]:
            return _Result("bravo-demo-project\n")
        return _Result("operator\n")

    monkeypatch.setattr(adapters_module, "_run", fake_run)
    assert isinstance(identity_for("aws"), AwsIdentity) and isinstance(identity_for("gcp"), GcpIdentity)
    assert identity_for("aws")({}) == {"account_id": "000000000000"}
    assert identity_for("gcp")({}) == {"project_id": "bravo-demo-project"}  # the active account is not bound
    assert ["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"] in calls
    cluster = FakeCluster()
    cluster.cluster_uid = lambda: "cluster-1"
    onprem = identity_for("onprem", cluster)
    assert isinstance(onprem, ClusterIdentity)
    assert onprem({"node_name": "n1"}) == {"cluster_uid": "cluster-1", "node_uid": "uid-n1"}
    assert onprem({}) == {"cluster_uid": "cluster-1"}
    with pytest.raises(StackError, match="cluster_arguments_required"):
        identity_for("onprem")
    with pytest.raises(StackError, match="unsupported_environment_kind"):
        identity_for("azure")


def test_gcp_identity_needs_an_active_credential(monkeypatch):
    monkeypatch.setattr(adapters_module, "_run", lambda command, **kw: _Result(
        "bravo-demo-project" if "get-value" in command else ""))
    with pytest.raises(StackError, match="gcp_identity_unavailable"):
        GcpIdentity()({})


# --- cluster binding and the end-to-end run of a cloud stack -------------------------------------------


class CloudCluster(FakeCluster):
    """A managed cluster: its kubeconfig server is the endpoint terraform reported (unless told otherwise)."""

    def __init__(self, server, **kw):
        super().__init__(**kw)
        self.secrets[("target-dev", "bravo-edge-tunnel-token")] = ("Opaque", {"token": "operator"})
        self._server = server

    def server(self):
        return self._server

    def cluster_uid(self):
        return "gke-cluster-uid"


def test_cluster_access_compares_kubeconfig_server_with_the_terraform_endpoint():
    stack = load_stack(REPO / "infra/stacks/cluster-gke/stack.json")
    run = {"stack": stack.document, "plan": {"request": {"params": {"side": "target"}}, "target": {}}}
    outputs = CachedOutputs(lambda params: lambda step: {"cluster": {"value": {"endpoint": "gke-1.example.goog"}}})
    assert ClusterChecks(CloudCluster("https://gke-1.example.goog"), outputs=outputs).check("cluster-access", run)
    assert ClusterChecks(CloudCluster("https://gke-1.example.goog:443/"), outputs=outputs).check("cluster-access", run)
    assert not ClusterChecks(CloudCluster("https://other.example.goog"), outputs=outputs).check("cluster-access", run)
    assert not ClusterChecks(CloudCluster("https://gke-1.example.goog")).check("cluster-access", run)  # no outputs
    empty = CachedOutputs(lambda params: lambda step: {})
    assert not ClusterChecks(CloudCluster("https://gke-1.example.goog"), outputs=empty).check("cluster-access", run)
    assert empty.values == {}  # an unapplied step is read again next time


def test_cloud_checks_read_the_stack_names():
    stack = load_stack(REPO / "infra/stacks/cluster-eks/stack.json")
    seen = []

    class Recorder(FakeCluster):
        def condition(self, kind, namespace, name, condition):
            seen.append((kind, namespace, name))
            return True

    run = {"stack": stack.document, "plan": {"request": {"params": {"side": "source"}}, "target": {}}}
    checks = ClusterChecks(Recorder())
    assert checks.check("secret-store-ready", run) and checks.check("target-checks", run)
    assert seen == [("SecretStore", "source-dev", "bravo-cloud-secrets"), ("Cluster", "source-dev", "source-db")]


TF = "services/infra-plan/tests/fixtures/tf/endpoint"
MINI = {
    "schema_version": 1, "stack": "cloud-test", "charts": {},
    "environment": {"kind": "gcp", "app_namespace": "${side}-dev", "database": "${side}-db",
                    "secret_store": {"kind": "SecretStore", "namespace": "${side}-dev", "name": "bravo-cloud-secrets"},
                    "cluster_endpoint": {"from": "cluster", "output": "cluster", "path": "endpoint"}},
    "params": {"private_dir": {"description": "operator files", "pattern": "^/.+$"},
               "state_dir": {"description": "local state", "pattern": "^/.+$"},
               "side": {"description": "role", "enum": ["source", "target"]}},
    "steps": [
        {"id": "cluster", "kind": "terraform", "owner": "A10", "description": "cluster", "dir": TF,
         "backend": {"file": "${private_dir}/backend.hcl", "config": {"path": "${state_dir}/cluster.tfstate"}},
         "var_file": "${private_dir}/cluster.tfvars.json"},
        {"id": "cluster-access", "kind": "operator_action", "owner": "A10", "description": "kubeconfig",
         "needs": ["cluster"], "instruction": "get-credentials", "confirm": "cluster-access"},
        {"id": "edge-token", "kind": "operator_action", "owner": "T17", "description": "token",
         "needs": ["cluster-access"], "instruction": "token", "confirm": "edge-token"},
        {"id": "db-secrets", "kind": "secret_generate", "owner": "T30", "description": "db",
         "needs": ["cluster-access"], "secrets": [{"namespace": "${side}-dev", "name": "${side}-app-db",
                                                    "type": "Opaque", "keys": {"password": {"random": 24}}}]},
        {"id": "verify", "kind": "probe", "owner": "A10", "description": "verify",
         "needs": ["edge-token", "db-secrets"], "check": "target-checks"},
    ],
}


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform binary not installed")
@pytest.mark.parametrize("server,expected", [("https://gke-1.example.goog", "SUCCEEDED"),
                                             ("https://some-other-cluster.example.goog", "WAITING_OPERATOR")])
def test_a_cloud_stack_builds_then_binds_the_cluster_by_endpoint(tmp_path, server, expected):
    from bravo_infra_plan.terraform import TerraformCli, outputs_reader

    private, state = tmp_path / "private", tmp_path / "state"
    private.mkdir()
    state.mkdir()
    (private / "backend.hcl").write_text("# local backend for the test\n")
    (private / "cluster.tfvars.json").write_text(json.dumps({"endpoint": "gke-1.example.goog"}))
    request = {"stack": "cloud-test", "environment_id": "env-gcp",
               "params": {"private_dir": str(private), "state_dir": str(state), "side": "target"}}
    stack, cli, approvals = parse_stack(MINI), TerraformCli(), FakeApprovals()
    steps = {s["id"]: s for s in MINI["steps"] if s["kind"] == "terraform"}
    cluster = CloudCluster(server)
    outputs = CachedOutputs(lambda params: outputs_reader(cli, REPO, steps, params))
    project = {"project_id": "bravo-demo-project"}
    executor = Executor(Planner(stack, REPO, terraform=cli), stack_revision=REV, cluster=cluster, repo=None,
                        approvals=approvals, checks=ClusterChecks(cluster, outputs=outputs),
                        store=FileRunStore(tmp_path / "runs"), repo_url="", identity=lambda params: dict(project),
                        sleep=lambda s: None, terraform=cli)
    approvals.approve("appr-1", executor.planner.plan(request, stack_revision=REV, target=project,
                                                      base_commit="0" * 40).plan_digest)
    run = executor.start(request=request, migration_plan_id="plan-demo", approval_id="appr-1", run_id="run-1",
                         started_by="applier")
    assert run["steps"]["cluster"]["status"] == "APPLIED"
    assert run["status"] == expected
    if expected == "SUCCEEDED":
        assert cluster.secrets[("target-dev", "target-app-db")][0] == "Opaque"  # templated namespace resolved
    else:  # a kubeconfig on another cluster: nothing is applied to it
        assert run["steps"]["cluster-access"]["status"] == "WAITING_OPERATOR"
        assert ("target-dev", "target-app-db") not in cluster.secrets
        assert {run["steps"][s]["status"] for s in ("edge-token", "db-secrets", "verify")} == {"PENDING"}
    assert "gke-1.example.goog" not in json.dumps(run)  # outputs stay in memory
