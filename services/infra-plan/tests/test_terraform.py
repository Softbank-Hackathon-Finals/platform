"""`terraform` steps with the real terraform binary and two local-backend stacks (no cloud).

The fixtures stand in for aws-foundation → source-eks: the second stack's vpc_id comes from the first
stack's sensitive output, in memory only.
"""

import copy
import json
import shutil
import subprocess

import pytest
from test_executor import FakeApprovals
from test_plan import REPO

from bravo_infra_plan import Planner, StackError, parse_stack
from bravo_infra_plan.adapters import FileRunStore
from bravo_infra_plan.executor import Executor
from bravo_infra_plan.terraform import TerraformCli, change_summary

pytestmark = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform binary not installed")
FIXTURES = "services/infra-plan/tests/fixtures/tf"
STACK = {
    "schema_version": 1, "stack": "aws-test", "charts": {},
    "params": {"private_dir": {"description": "operator files outside the repository", "pattern": "^/.+$"},
               "state_dir": {"description": "local state for the test", "pattern": "^/.+$"},
               "cluster_name": {"description": "cluster name", "pattern": "^[a-z][a-z0-9-]{2,30}$"}},
    "steps": [
        {"id": "network", "kind": "terraform", "owner": "T1", "description": "network",
         "dir": f"{FIXTURES}/network",
         "backend": {"file": "${private_dir}/backend.hcl", "config": {"path": "${state_dir}/network.tfstate"}},
         "var_file": "${private_dir}/network.tfvars.json"},
        {"id": "cluster", "kind": "terraform", "owner": "T6", "description": "cluster", "needs": ["network"],
         "dir": f"{FIXTURES}/cluster",
         "backend": {"file": "${private_dir}/backend.hcl", "config": {"path": "${state_dir}/cluster.tfstate"}},
         "vars": {"vpc_id": {"from": "network", "output": "foundation", "path": "vpcs.source.id"},
                  "name": "${cluster_name}"}},
    ],
}
ACCOUNT = {"account_id": "000000000000", "region": "test"}


@pytest.fixture
def aws(tmp_path):
    private, state = tmp_path / "private", tmp_path / "state"
    private.mkdir()
    state.mkdir()
    (private / "backend.hcl").write_text("# local backend for the test; the real file names the S3 bucket\n")
    (private / "network.tfvars.json").write_text(json.dumps({"cidr": "10.80.0.0/16"}))
    request = {"stack": "aws-test", "environment_id": "env-aws",
               "params": {"private_dir": str(private), "state_dir": str(state), "cluster_name": "bravo-source"}}
    approvals = FakeApprovals()

    def executor(document=STACK):
        planner = Planner(parse_stack(document), REPO, terraform=TerraformCli())
        return Executor(planner, stack_revision="1" * 40, cluster=None, repo=None, approvals=approvals, checks=None,
                        store=FileRunStore(tmp_path / "runs"), repo_url="", identity=lambda params: dict(ACCOUNT),
                        sleep=lambda s: None)

    def run(run_id, approval_id, document=STACK):
        ex = executor(document)
        approvals.approve(approval_id, ex.planner.plan(request, stack_revision="1" * 40, target=ACCOUNT,
                                                       base_commit="0" * 40).plan_digest)
        return ex.start(request=request, migration_plan_id="plan-demo", approval_id=approval_id, run_id=run_id,
                        started_by="applier")

    return dict(run=run, executor=executor, private=private, state=state, tmp=tmp_path, request=request,
                approvals=approvals)


def state_value(path, address):
    data = json.loads(path.read_text())
    for resource in data["resources"]:
        if f'{resource["type"]}.{resource["name"]}' == address:
            return resource["instances"][0]["attributes"]["input"]["value"]


def build(aws):
    """A fresh build takes two approvals: the cluster's change set exists only once the network is applied."""
    first = aws["run"]("run-1", "appr-1")
    assert first["steps"]["network"]["status"] == "APPLIED"
    assert (first["steps"]["cluster"]["status"], first["steps"]["cluster"]["reason"]) == (
        "HOLD", "terraform_change_set_unapproved")
    assert next(s for s in first["plan"]["steps"] if s["id"] == "cluster")["change_set"] == "PENDING_UPSTREAM"
    second = aws["run"]("run-2", "appr-2")
    assert {k: v["status"] for k, v in second["steps"].items()} == {"network": "SKIPPED", "cluster": "APPLIED"}
    return first, second


def test_outputs_flow_to_the_next_stack_in_memory_and_a_rerun_changes_nothing(aws):
    run, _ = build(aws)
    vpc_id = state_value(aws["state"] / "cluster.tfstate", "terraform_data.cluster")["vpc_id"]
    assert vpc_id.startswith("vpc-")
    record = (aws["tmp"] / "runs" / "run-2.json").read_text()
    assert vpc_id not in record and "10.80.0.0/16" not in record  # wired values and private inputs stay out
    assert run["terraform"]["network"]["changes"] == {"create": 1}
    approved = next(s for s in run["plan"]["steps"] if s["id"] == "network")
    assert approved["changes"] == {"create": 1} and len(approved["change_set"]) == 64  # seen before approval

    again = aws["run"]("run-3", "appr-3")
    assert {k: v["status"] for k, v in again["steps"].items()} == {"network": "SKIPPED", "cluster": "SKIPPED"}


def test_a_private_input_changed_after_approval_holds_before_planning(aws):
    ex = aws["executor"]()
    aws["approvals"].approve("appr-1", ex.planner.plan(aws["request"], stack_revision="1" * 40, target=ACCOUNT,
                                                       base_commit="0" * 40).plan_digest)
    original_plan = ex.planner.plan
    aws["private"].joinpath("network.tfvars.json").write_text(json.dumps({"cidr": "10.99.0.0/16"}))
    ex.planner.plan = lambda *a, **k: original_plan(*a, **k)  # start re-renders: the digest moved
    run = ex.start(request=aws["request"], migration_plan_id="plan-demo", approval_id="appr-1", run_id="run-1",
                   started_by="applier")
    assert (run["status"], run["reason"]) == ("REFUSED", "plan_digest_mismatch")
    assert not (aws["state"] / "network.tfstate").exists()


def test_a_plan_that_replaces_or_deletes_is_held_for_a_person(aws):
    build(aws)
    aws["private"].joinpath("network.tfvars.json").write_text(json.dumps({"cidr": "10.99.0.0/16"}))
    run = aws["run"]("run-3", "appr-3")  # approved with the replace in view, but deletes stay with people
    assert run["steps"]["network"]["status"] == "HOLD"
    assert run["steps"]["network"]["reason"] == "terraform_plan_destroys"
    assert run["terraform"]["network"] == {"changes": {"replace": 1}, "destructive": 1}
    assert run["steps"]["cluster"]["status"] == "PENDING"
    assert state_value(aws["state"] / "network.tfstate", "terraform_data.vpc") == "10.80.0.0/16"  # unchanged


def test_terraform_stacks_are_checked_before_anything_runs(aws, tmp_path):
    broken = copy.deepcopy(STACK)
    broken["steps"][1]["needs"] = []
    with pytest.raises(StackError, match="terraform_reference_without_need"):
        parse_stack(broken)
    inside = copy.deepcopy(aws["request"])
    inside["params"]["private_dir"] = str(REPO / "services/infra-plan")  # private files must not live in Git
    with pytest.raises(StackError, match="terraform_private_input_unavailable"):
        aws["executor"]().planner.plan(inside, stack_revision="1" * 40, target=ACCOUNT, base_commit="0" * 40)


def test_code_digest_covers_sourced_modules():
    from bravo_infra_plan.terraform import code_digest

    digest = code_digest(REPO, "infra/envs/aws-foundation")  # sources ../../modules/network/aws
    assert len(digest) == 64
    assert subprocess.run(["git", "-C", str(REPO), "ls-files", "infra/modules/network/aws"],
                          capture_output=True, text=True).stdout


def test_the_aws_stack_wires_only_declared_variables_and_digests_every_env(tmp_path):
    import re

    from bravo_infra_plan import load_stack

    stack = load_stack(REPO / "infra/stacks/aws-platform/stack.json")
    for step in stack.document["steps"]:
        declared = set(re.findall(r'^variable "([^"]+)"', "".join(
            p.read_text() for p in (REPO / step["dir"]).glob("*.tf")), re.M))
        assert set(step.get("vars", {})) <= declared, step["id"]
        (tmp_path / f'{step["id"]}.backend.hcl').write_text("# private\n")
        (tmp_path / f'{step["id"]}.tfvars.json').write_text("{}")
    request = {"stack": "aws-platform", "environment_id": "env-aws", "params": {"private_dir": str(tmp_path)}}
    plan = Planner(stack, REPO).plan(request, stack_revision="1" * 40, target=ACCOUNT, base_commit="0" * 40)
    assert len({s["inputs"]["code"] for s in plan.document["steps"]}) == len(plan.document["steps"])
    assert plan.document["levels"][0] == ["aws-foundation", "cost-aws", "platform-audit"]
    (tmp_path / "source-eks.tfvars.json").write_text('{"instance_type": "t3.xlarge"}')
    changed = Planner(stack, REPO).plan(request, stack_revision="1" * 40, target=ACCOUNT, base_commit="0" * 40)
    assert changed.plan_digest != plan.plan_digest  # the private input is bound by digest


def test_verify_plans_in_order_against_state_and_never_applies(aws):
    from bravo_infra_plan.terraform import verify

    build(aws)
    before = (aws["state"] / "network.tfstate").read_text()
    stack = parse_stack(STACK)
    assert [r["status"] for r in verify(stack, REPO, aws["request"]["params"])] == ["NO_CHANGES", "NO_CHANGES"]
    aws["private"].joinpath("network.tfvars.json").write_text(json.dumps({"cidr": "10.99.0.0/16"}))
    network = verify(stack, REPO, aws["request"]["params"], only=["network"])
    assert network == [{"step": "network", "status": "CHANGES", "changes": {"replace": 1}, "destructive": 1}]
    assert (aws["state"] / "network.tfstate").read_text() == before  # plan only: state untouched
    assert not list(aws["state"].glob("*.lock*"))


def test_a_change_set_that_moved_after_approval_is_refused(aws):
    """Someone applied the network out of band between approval and run: the change set is no longer
    the one a person saw, so the run is refused before anything is applied."""
    from bravo_infra_plan.terraform import Scratch, outputs_reader, plan_step

    ex = aws["executor"]()
    aws["approvals"].approve("appr-1", ex.planner.plan(aws["request"], stack_revision="1" * 40, target=ACCOUNT,
                                                       base_commit="0" * 40).plan_digest)
    cli, steps = TerraformCli(), {s["id"]: s for s in STACK["steps"]}
    with Scratch() as scratch:
        planned = plan_step(cli, REPO, steps["network"], aws["request"]["params"],
                            outputs_reader(cli, REPO, steps, aws["request"]["params"]), scratch, lock=True)
        cli.apply(planned["directory"], scratch / "data", scratch / "plan.bin")
    run = ex.start(request=aws["request"], migration_plan_id="plan-demo", approval_id="appr-1", run_id="run-1",
                   started_by="applier")
    assert (run["status"], run["reason"]) == ("REFUSED", "plan_digest_mismatch")


def test_forget_and_delete_are_destructive_and_reads_are_not_changes():
    plan = {"resource_changes": [
        {"address": "a.keep", "change": {"actions": ["forget"]}},
        {"address": "b.gone", "change": {"actions": ["delete"]}},
        {"address": "c.new", "change": {"actions": ["create"]}},
        {"address": "d.data", "change": {"actions": ["read"]}},
        {"address": "e.same", "change": {"actions": ["no-op"]}}]}
    counts, destructive, change_set = change_summary(plan)
    assert destructive == ["a.keep", "b.gone"]
    assert counts == {"forget": 1, "delete": 1, "create": 1}
    assert [c["address"] for c in change_set] == ["a.keep", "b.gone", "c.new"]
