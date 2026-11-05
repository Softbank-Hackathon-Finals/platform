"""bravo-apply: check an approved change, apply it pinned to the approved digest, report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .binding import BindingError, diff_hash, verify_claim
from .deploy import FakeKube, KubectlKube, image_patch
from .path import apply_approved_change


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _evidence(args) -> dict:
    """Either the exact candidate diff bytes or the snapshot sources of the approved files."""
    out = {}
    if args.diff:
        out["diff"] = Path(args.diff).read_bytes()
    if args.sources:
        root = Path(args.sources)
        out["sources"] = {e["path"]: (root / e["path"]).read_text(encoding="utf-8") for e in _load(args.proposal)["edits"]}
    return out


def cmd_check(args) -> int:
    try:
        binding = verify_claim(_load(args.claim), _load(args.proposal), target_environment_id=args.target_env, now=datetime.now(UTC), **_evidence(args))
    except BindingError as exc:
        print(json.dumps({"ok": False, "reason": exc.code}))
        return 1
    print(json.dumps({"ok": True, **binding, "patch": image_patch(args.repository, binding["image_digest"], args.container)}, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    if args.kube == "fake":
        kube = FakeKube(f"{args.repository}@sha256:{'0' * 64}")
    else:
        kube = KubectlKube(args.kubeconfig)
    result = apply_approved_change(_load(args.claim), _load(args.proposal), kube=kube, target_environment_id=args.target_env,
                                   namespace=args.namespace, deployment=args.deployment, container=args.container,
                                   repository=args.repository, receipts_dir=Path(args.receipts), **_evidence(args))
    print(json.dumps({k: v for k, v in result.as_dict().items() if k != "steps"}, ensure_ascii=False))
    for step in result.steps:
        print(json.dumps(step, ensure_ascii=False), file=sys.stderr)
    return {"APPLIED": 0, "REFUSED": 2, "SAFE_HOLD": 3}[result.outcome]


def cmd_diff_hash(args) -> int:
    print(diff_hash(Path(args.diff).read_bytes()))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bravo-apply")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("check", cmd_check), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--claim", required=True, help="T10 claim_application receipt JSON")
        p.add_argument("--proposal", required=True, help="approved T11 proposal JSON")
        p.add_argument("--target-env", required=True)
        p.add_argument("--repository", required=True, help="image repository without tag, e.g. ghcr.io/org/demo-chat")
        p.add_argument("--container", default="app")
        p.add_argument("--diff", default="", help="candidate diff bytes handed off by T15")
        p.add_argument("--sources", default="", help="snapshot root holding the approved files (to recompute the diff)")
        if name == "run":
            p.add_argument("--kube", choices=["fake", "kubectl"], default="fake")
            p.add_argument("--kubeconfig", default="")
            p.add_argument("--namespace", required=True)
            p.add_argument("--deployment", required=True)
            p.add_argument("--receipts", required=True)
        p.set_defaults(func=func)
    d = sub.add_parser("diff-hash")
    d.add_argument("--diff", required=True)
    d.set_defaults(func=cmd_diff_hash)
    args = parser.parse_args(argv)
    if getattr(args, "kube", None) == "kubectl" and not args.kubeconfig:
        parser.error("--kubeconfig is required with --kube kubectl")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
