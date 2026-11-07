"""bravo-infra-plan: check a stack, print its request schema, or render a plan offline.

`plan` never contacts a cluster or a Git remote. The caller supplies the observed target identity
and the configuration repository's base commit; the API (next step of T44) reads them itself.
"""

import argparse
import json
import sys
from pathlib import Path

from .plan import HelmChartRenderer, Planner
from .stack import StackError, load_stack


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
    args = parser.parse_args(argv)
    try:
        stack = load_stack(args.stack)
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
        result = Planner(stack, args.repo_root, chart_renderer=HelmChartRenderer(args.charts_dir)).plan(
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
