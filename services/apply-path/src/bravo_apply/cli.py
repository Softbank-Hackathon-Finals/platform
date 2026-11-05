"""bravo-apply: check an approved change, apply it pinned to the approved digest, report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .binding import IDENTIFIER, BindingError, diff_hash, verify_claim
from .deploy import FakeKube, KubectlKube, image_patch
from .path import ApplyOutcome, RecordUnreadable, apply, get_result

EXIT = {"APPLIED": 0, "REFUSED": 2, "SAFE_HOLD": 3, "UNKNOWN": 4}
EXIT_NOT_STARTED = 5
EXIT_UNREADABLE = 6
FAKE_WARNING = "FAKE_CLUSTER: in-memory FakeKube, nothing was deployed to any real cluster"


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


def _emit(result: ApplyOutcome, *, t10: bool = False) -> int:
    """stdout: the record without steps (or the T10 view); stderr: steps. Exit code by outcome."""
    if t10:
        doc = {"application": result.t10_application(), "failure": result.failure_report()}
    else:
        doc = {k: v for k, v in result.as_dict().items() if k != "steps"}
    if result.cluster == "fake":
        doc["warning"] = FAKE_WARNING
        print(f"WARNING {FAKE_WARNING}", file=sys.stderr)
    # sorted keys: a re-call or get-result prints byte-identical output to the first run
    print(json.dumps(doc, ensure_ascii=False, sort_keys=True))
    if not t10:
        for step in result.steps:
            print(json.dumps(step, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return EXIT[result.outcome]


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
    receipts = Path(args.receipts)
    try:
        if get_result(args.application_id, receipts) is not None:
            print("NOTE existing record returned unchanged; no cluster call made", file=sys.stderr)
        result = apply(args.application_id, _load(args.claim), _load(args.proposal), kube=kube, receipts_dir=receipts,
                       target_environment_id=args.target_env, namespace=args.namespace, deployment=args.deployment,
                       container=args.container, repository=args.repository, timeout_seconds=args.timeout, **_evidence(args))
    except RecordUnreadable as exc:
        print(json.dumps({"application_id": args.application_id, "outcome": None, "reason": "RECORD_UNREADABLE", "detail": str(exc)}))
        return EXIT_UNREADABLE
    return _emit(result)


def cmd_get_result(args) -> int:
    try:
        result = get_result(args.application_id, Path(args.receipts))
    except RecordUnreadable as exc:
        print(json.dumps({"application_id": args.application_id, "outcome": None, "reason": "RECORD_UNREADABLE", "detail": str(exc)}))
        return EXIT_UNREADABLE
    if result is None:
        print(json.dumps({"application_id": args.application_id, "outcome": None, "reason": "NOT_STARTED"}))
        return EXIT_NOT_STARTED
    return _emit(result, t10=args.t10)


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
            p.add_argument("--application-id", required=True, help="the T10 claim's application_id; must equal claim.application_id")
            p.add_argument("--kube", choices=["kubectl", "fake"], required=True,
                           help="kubectl = real cluster via --kubeconfig; fake = in-memory, deploys nothing")
            p.add_argument("--kubeconfig", default="")
            p.add_argument("--namespace", required=True)
            p.add_argument("--deployment", required=True)
            p.add_argument("--receipts", required=True, help="durable directory holding one record per application_id")
            p.add_argument("--timeout", type=int, default=300, help="rollout wait in seconds")
        p.set_defaults(func=func)
    g = sub.add_parser("get-result", help="read the recorded outcome; never calls the cluster")
    g.add_argument("--application-id", required=True)
    g.add_argument("--receipts", required=True)
    g.add_argument("--t10", action="store_true", help="print {application, failure} as T10 consumes them")
    g.set_defaults(func=cmd_get_result)
    d = sub.add_parser("diff-hash")
    d.add_argument("--diff", required=True)
    d.set_defaults(func=cmd_diff_hash)
    args = parser.parse_args(argv)
    if getattr(args, "kube", None) == "kubectl" and not args.kubeconfig:
        parser.error("--kubeconfig is required with --kube kubectl")
    if getattr(args, "application_id", None) is not None and not IDENTIFIER.fullmatch(args.application_id):
        parser.error("--application-id must match " + IDENTIFIER.pattern)
    if getattr(args, "timeout", 1) < 1:
        parser.error("--timeout must be at least 1 second")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
