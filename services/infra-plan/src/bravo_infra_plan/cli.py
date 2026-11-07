"""bravo-infra-plan: check a stack, print its request schema, render a plan, or run an approved one.

`plan` never contacts a cluster or a Git remote; the caller supplies the target identity and the
configuration repository's base commit. `run` / `confirm` / `status` drive the executor with real
adapters. The T10 token is read from an environment variable, never from an argument.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .adapters import (
    AuditLedger,
    AwsIdentity,
    ClusterChecks,
    FileRunStore,
    GitConfigRepo,
    KubectlCluster,
    T10Approvals,
)
from .executor import Executor
from .plan import HelmChartRenderer, Planner
from .stack import StackError, load_stack
from .terraform import TerraformCli


def _executor(args, stack):
    repo_root = Path(args.repo_root).resolve()
    revision = args.stack_revision or subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
                                                     capture_output=True, text=True).stdout.strip()
    token = os.environ.get(args.t10_token_env, "")
    if not token:
        raise StackError("t10_token_required")
    approvals, store = T10Approvals(args.t10_url, token), FileRunStore(args.runs_dir)
    ledger = (AuditLedger(args.audit_sink, store) if args.audit_sink
              else _jsonl(Path(args.runs_dir) / "ledger.jsonl"))
    if args.aws:  # a terraform stack: bound to the AWS account, no cluster or configuration repository
        return Executor(Planner(stack, repo_root, terraform=TerraformCli()), stack_revision=revision,
                        cluster=None, repo=None,
                        approvals=approvals, checks=None, store=store, repo_url="", ledger=ledger,
                        identity=AwsIdentity())
    if not (args.kubeconfig and args.config_repo and args.config_workdir and args.charts_dir):
        raise StackError("cluster_arguments_required")
    cluster = KubectlCluster(args.kubeconfig)
    return Executor(Planner(stack, repo_root, chart_renderer=HelmChartRenderer(args.charts_dir)),
                    stack_revision=revision, cluster=cluster,
                    repo=GitConfigRepo(args.config_repo, args.config_branch, args.config_workdir),
                    approvals=approvals, checks=ClusterChecks(cluster), store=store,
                    repo_url=args.argo_repo_url or args.config_repo, ledger=ledger)


def _jsonl(path):
    def append(event):
        with open(path, "a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    return append


def _summary(run):
    steps = {k: v["status"] for k, v in run["steps"].items()}
    return {"run_id": run["run_id"], "status": run["status"], "reason": run.get("reason"),
            "plan_digest": run["plan_digest"], "waiting": [k for k, v in steps.items() if v == "WAITING_OPERATOR"],
            "held": [k for k, v in steps.items() if v in ("HOLD", "FAILED")],
            "counts": {s: list(steps.values()).count(s) for s in sorted(set(steps.values()))}}


def _executor_args(parser):
    for flag in ("--repo-root", "--runs-dir", "--t10-url"):
        parser.add_argument(flag, required=True)
    for flag in ("--charts-dir", "--kubeconfig", "--config-repo", "--config-workdir"):
        parser.add_argument(flag, help="cluster stacks only")
    parser.add_argument("--aws", action="store_true", help="a terraform stack bound to the AWS account (STS)")
    parser.add_argument("--audit-sink", help="T24 ledger: ./store or s3://bucket/audit (default: runs-dir JSONL)")
    parser.add_argument("--config-branch", default="main")
    parser.add_argument("--argo-repo-url", help="URL the destination Argo CD reads (default: --config-repo)")
    parser.add_argument("--stack-revision", help="default: git rev-parse HEAD of --repo-root")
    parser.add_argument("--t10-token-env", default="BRAVO_T10_TOKEN")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bravo-infra-plan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate a stack and print its parallel levels")
    check.add_argument("stack")
    schema = sub.add_parser("request-schema", help="print the plan request JSON Schema")
    schema.add_argument("stack")
    plan = sub.add_parser("plan", help="render a plan into a new output directory")
    plan.add_argument("stack")
    plan.add_argument("--request", required=True)
    plan.add_argument("--repo-root", required=True)
    plan.add_argument("--charts-dir", required=True)
    plan.add_argument("--stack-revision", required=True)
    plan.add_argument("--cluster-uid", required=True)
    plan.add_argument("--node-uid", required=True)
    plan.add_argument("--base-commit", required=True)
    plan.add_argument("--out", required=True)
    run = sub.add_parser("run", help="apply an approved plan (T10 approval_kind=infra)")
    run.add_argument("stack")
    run.add_argument("--request", required=True)
    run.add_argument("--migration-plan-id", required=True)
    run.add_argument("--approval-id", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--started-by", required=True)
    _executor_args(run)
    confirm = sub.add_parser("confirm", help="record that a human finished an operator step, then resume")
    confirm.add_argument("stack")
    confirm.add_argument("--run-id", required=True)
    confirm.add_argument("--step", required=True)
    confirm.add_argument("--confirmed-by", required=True)
    _executor_args(confirm)
    verify = sub.add_parser("verify", help="plan every terraform step against real state; never apply or lock")
    verify.add_argument("stack")
    verify.add_argument("--request", required=True)
    verify.add_argument("--repo-root", required=True)
    verify.add_argument("--only", nargs="*", help="step ids to check (default: all)")
    status = sub.add_parser("status", help="print a run summary")
    status.add_argument("--runs-dir", required=True)
    status.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(_summary(FileRunStore(args.runs_dir).load(args.run_id)), indent=2))
            return 0
        stack = load_stack(args.stack)
        if args.command == "verify":
            from .terraform import verify

            request = stack.validate_request(json.loads(Path(args.request).read_text()))
            results = verify(stack, Path(args.repo_root).resolve(), request["params"], only=args.only)
            print(json.dumps(results, indent=2))
            return 0 if all(r["status"] == "NO_CHANGES" for r in results) else 3
        if args.command == "run":
            result = _executor(args, stack).start(
                request=json.loads(Path(args.request).read_text()), migration_plan_id=args.migration_plan_id,
                approval_id=args.approval_id, run_id=args.run_id, started_by=args.started_by)
            print(json.dumps(_summary(result), indent=2))
            return 0 if result["status"] in ("SUCCEEDED", "WAITING_OPERATOR") else 3
        if args.command == "confirm":
            result = _executor(args, stack).confirm(args.run_id, args.step, confirmed_by=args.confirmed_by)
            print(json.dumps(_summary(result), indent=2))
            return 0 if result["status"] in ("SUCCEEDED", "WAITING_OPERATOR") else 3
        if args.command == "check":
            for number, level in enumerate(stack.levels, 1):
                print(f"{number:2} {' '.join(level)}")
            return 0
        if args.command == "request-schema":
            print(json.dumps(stack.request_schema(), indent=2))
            return 0
        out = Path(args.out)
        if out.exists() and any(out.iterdir()):
            raise StackError("output_directory_not_empty")
        has_terraform = any(s["kind"] == "terraform" for s in stack.document["steps"])
        result = Planner(stack, args.repo_root, chart_renderer=HelmChartRenderer(args.charts_dir),
                         terraform=TerraformCli() if has_terraform else None).plan(
            json.loads(Path(args.request).read_text()), stack_revision=args.stack_revision,
            target={"cluster_uid": args.cluster_uid, "node_uid": args.node_uid}, base_commit=args.base_commit)
        for step_id, rendered in result.files.items():
            kind = stack.steps[step_id]["kind"]
            for name, text in rendered.items():
                path = out / ("tree" if kind == "gitops_publish" else f"apply/{step_id}") / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        (out / "plan.json").write_text(json.dumps(result.document, indent=2, sort_keys=True) + "\n")
        print(result.plan_digest)
        return 0
    except StackError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
