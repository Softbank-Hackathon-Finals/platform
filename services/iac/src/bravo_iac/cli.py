"""bravo-iac: catalog, check, plan and stage a desired-state change for a GitOps PR.

    bravo-iac modules
    bravo-iac check infra/iac/environments/target-dev.json
    bravo-iac plan --current infra/iac/environments/target-dev.json --proposed proposed.json [--origin agent]
    bravo-iac stage --plan plan.json --proposed proposed.json --target infra/iac/environments/target-dev.json

stage writes the proposed state into the target file only if the file still has the plan's
base_digest and the proposal its proposed_digest, and the plan was accepted. Opening the PR,
review and merge stay with people (GitOps); nothing here touches a cluster.
Exit: 0 done, 1 refused or invalid (one fixed code), 2 usage.
"""

import argparse
import json
import sys
from pathlib import Path

from . import catalog
from .modules import IacError
from .plan import canonical, digest, plan


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(args) -> dict:
    if args.command == "modules":
        return {"modules": catalog.describe()}
    if args.command == "check":
        state = catalog.check_state(_load(args.state))
        return {"environment": state["environment"], "digest": digest(state)}
    if args.command == "plan":
        return plan(_load(args.current), _load(args.proposed), origin=args.origin)
    planned, proposed = _load(args.plan), catalog.check_state(_load(args.proposed))
    current = catalog.check_state(_load(args.target))
    if planned.get("kind") != "iac-plan.v1" or planned.get("accepted") is not True:
        raise IacError("IAC_PLAN_NOT_ACCEPTED")
    if digest(current) != planned.get("base_digest"):
        raise IacError("IAC_BASE_CHANGED")  # Git moved since the plan: plan again
    if digest(proposed) != planned.get("proposed_digest"):
        raise IacError("IAC_PROPOSAL_CHANGED")
    Path(args.target).write_text(json.dumps(proposed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"staged": args.target, "plan_digest": planned["plan_digest"], "proposed_digest": digest(proposed)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bravo-iac", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("modules")
    c = sub.add_parser("check")
    c.add_argument("state")
    pl = sub.add_parser("plan")
    pl.add_argument("--current", required=True)
    pl.add_argument("--proposed", required=True)
    pl.add_argument("--origin", choices=["agent", "operator"], default="operator")
    st = sub.add_parser("stage")
    st.add_argument("--plan", required=True)
    st.add_argument("--proposed", required=True)
    st.add_argument("--target", required=True)
    args = p.parse_args(argv)
    try:
        result = run(args)
    except IacError as error:
        print(json.dumps({"status": "REFUSED", "code": error.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "REFUSED", "code": "IAC_INPUT_UNREADABLE"}))
        return 1
    print(canonical(result).decode() if args.command == "plan" else json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if args.command != "plan" or result["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
