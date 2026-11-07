"""bravo-iac: catalog, check, plan and stage a desired-state change for a GitOps PR.

    bravo-iac modules
    bravo-iac check infra/iac/environments/target-dev.json
    bravo-iac plan --current infra/iac/environments/target-dev.json --proposed proposed.json [--origin agent]
    bravo-iac stage --plan plan.json --proposed proposed.json --target infra/iac/environments/target-dev.json
    bravo-iac tailnet-policy infra/iac/environments/*.json > infra/tailnet/routes-policy.json
    bravo-iac tailnet-check --policy <tailnet policy JSON> infra/iac/environments/*.json
    bravo-iac tailnet-verify infra/iac/environments/*.json      # live, through this device's Tailscale
    bravo-iac chart infra/iac/environments/target-dev.json --out infra/iac/charts/target-dev

stage writes the proposed state into the target file only if the file still has the plan's
base_digest and the proposal its proposed_digest, and the plan was accepted. Opening the PR,
review and merge stay with people (GitOps); nothing here touches a cluster.
Exit: 0 done, 1 refused or invalid (one fixed code), 2 usage.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import catalog, chart, tailnet
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
    if args.command == "chart":
        written = []
        for name, content in chart.files(_load(args.state)).items():
            path = Path(args.out) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written.append(name)
        return {"chart": args.out, "files": written}
    if args.command == "tailnet-policy":
        return tailnet.fragment([_load(s) for s in args.states])
    if args.command == "tailnet-check":
        missing = tailnet.check_policy(_load(args.policy), [_load(s) for s in args.states])
        return {"missing": missing, "ok": not missing}
    if args.command == "tailnet-verify":
        peers = tailnet.status(Path(args.status_json).read_text() if args.status_json else None)
        rows = tailnet.verify([_load(s) for s in args.states], peers)
        return {"rows": rows, "ok": not any(r["result"] in ("OFFLINE", "TAG_MISMATCH") for r in rows)}
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
    ch = sub.add_parser("chart")
    ch.add_argument("state")
    ch.add_argument("--out", required=True)
    tp = sub.add_parser("tailnet-policy")
    tp.add_argument("states", nargs="+")
    tc = sub.add_parser("tailnet-check")
    tc.add_argument("--policy", required=True)
    tc.add_argument("states", nargs="+")
    tv = sub.add_parser("tailnet-verify")
    tv.add_argument("--status-json", help="saved `tailscale status --json` (default: ask this device)")
    tv.add_argument("states", nargs="+")
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
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"status": "REFUSED", "code": "IAC_INPUT_UNREADABLE"}))
        return 1
    if args.command in ("plan",):
        print(canonical(result).decode())
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.command == "plan":
        return 0 if result["accepted"] else 1
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
