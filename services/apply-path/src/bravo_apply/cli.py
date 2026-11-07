"""bravo-apply: check an approved change, apply it (image digest or bucket policy), report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .binding import BUCKET_POLICY, IDENTIFIER, BindingError, diff_hash, verify_claim
from .deploy import FakeKube, KubectlKube, image_patch
from .desired import DesiredRefused, apply_desired_exposure
from .path import ApplyOutcome, RecordUnreadable, apply, get_result
from .policy import (
    APP_BUCKET,
    AUTH_SECRET,
    CONTAINER,
    WORKLOAD,
    PolicyRefused,
    PolicyTarget,
    translate_policy,
)

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


def _policy_target(args) -> PolicyTarget | None:
    """The bucket-policy target, only when the operator configured one (``--object-endpoint``)."""
    if not args.object_endpoint:
        return None
    return PolicyTarget(namespace=args.object_namespace or args.namespace, endpoint=args.object_endpoint,
                        probe_key=args.probe_key, bucket=args.bucket, policy_bucket=args.policy_bucket or None,
                        secret=args.auth_secret, workload=args.object_workload, container=args.object_container)


def cmd_check(args) -> int:
    proposal = _load(args.proposal)
    try:
        binding = verify_claim(_load(args.claim), proposal, target_environment_id=args.target_env, now=datetime.now(UTC), **_evidence(args))
        if binding["change_kind"] == BUCKET_POLICY:
            state = translate_policy(proposal["edits"][0]["replacement"], policy_bucket=args.policy_bucket or args.bucket)
            print(json.dumps({"ok": True, **binding, "policy_state": state}, ensure_ascii=False))
            return 0
    except (BindingError, PolicyRefused) as exc:
        print(json.dumps({"ok": False, "reason": exc.code}))
        return 1
    if not args.repository:
        print(json.dumps({"ok": False, "reason": "IMAGE_TARGET_MISSING"}))
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
                       container=args.container, repository=args.repository, policy_target=_policy_target(args),
                       timeout_seconds=args.timeout, **_evidence(args))
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


DESIRED_EXIT = {"APPLIED": 0, "ALREADY_CLOSED": 0, "REFUSED": 2, "SAFE_HOLD": 3, "STARTED": 4}


def cmd_desired_exposure(args) -> int:
    """Apply a reviewed T44 ``closed`` desired state to the target Secret (bucket-policy branch)."""
    try:
        target = PolicyTarget(namespace=args.object_namespace or args.namespace, endpoint=args.object_endpoint,
                              probe_key=args.probe_key, bucket=args.bucket, secret=args.auth_secret,
                              workload=args.object_workload, container=args.object_container)
        record = apply_desired_exposure(args.environment, args.revision, reviewed_ref=args.reviewed_ref,
                                        repo_root=Path(args.repo_root), kube=KubectlKube(args.kubeconfig),
                                        receipts_dir=Path(args.receipts), target=target,
                                        plan_id=args.plan_id or None, timeout_seconds=args.timeout)
    except (DesiredRefused, PolicyRefused) as exc:
        print(json.dumps({"status": "REFUSED", "reason": exc.code, "recorded": False}, sort_keys=True))
        return EXIT["REFUSED"]
    except ValueError as exc:  # target configuration
        print(json.dumps({"status": "REFUSED", "reason": str(exc), "recorded": False}, sort_keys=True))
        return EXIT["REFUSED"]
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return DESIRED_EXIT[record["status"]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bravo-apply")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("check", cmd_check), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--claim", required=True, help="T10 claim_application receipt JSON")
        p.add_argument("--proposal", required=True, help="approved T11 proposal JSON")
        p.add_argument("--target-env", required=True)
        p.add_argument("--repository", default="", help="app image change: image repository without tag, e.g. ghcr.io/org/demo-chat")
        p.add_argument("--container", default="app")
        o = p.add_argument_group("bucket-policy change (used when the binding's change_kind is bucket_policy)")
        o.add_argument("--object-endpoint", default="", help="S3 endpoint of the target SeaweedFS reachable from here, scheme://host:port")
        o.add_argument("--probe-key", default="", help="an existing object in --bucket; read unsigned and signed after the change")
        o.add_argument("--bucket", default=APP_BUCKET, help="application bucket whose anonymous access is closed")
        o.add_argument("--policy-bucket", default="", help="bucket named in the approved policy (the rehearsal bucket); default --bucket")
        o.add_argument("--object-namespace", default="", help="namespace of the SeaweedFS Secret and workload; default --namespace")
        o.add_argument("--auth-secret", default=AUTH_SECRET)
        o.add_argument("--object-workload", default=WORKLOAD)
        o.add_argument("--object-container", default=CONTAINER)
        p.add_argument("--diff", default="", help="candidate diff bytes handed off by T15")
        p.add_argument("--sources", default="", help="snapshot root holding the approved files (to recompute the diff)")
        if name == "run":
            p.add_argument("--application-id", required=True, help="the T10 claim's application_id; must equal claim.application_id")
            p.add_argument("--kube", choices=["kubectl", "fake"], required=True,
                           help="kubectl = real cluster via --kubeconfig; fake = in-memory, deploys nothing")
            p.add_argument("--kubeconfig", default="")
            p.add_argument("--namespace", required=True)
            p.add_argument("--deployment", default="", help="app image change: the app Deployment")
            p.add_argument("--receipts", required=True, help="durable directory holding one record per application_id")
            p.add_argument("--timeout", type=int, default=300, help="rollout wait in seconds")
        p.set_defaults(func=func)
    x = sub.add_parser("desired-exposure", help="apply a reviewed T44 closed desired state (bucket identities), "
                                                "then request the IAM-01.1 recheck; never opens")
    x.add_argument("--environment", required=True, help="T44 environment id, e.g. target-dev")
    x.add_argument("--revision", required=True, help="full commit SHA holding the desired state (the promoted one)")
    x.add_argument("--reviewed-ref", required=True, help="protected ref the revision must be reachable from")
    x.add_argument("--repo-root", required=True, help="a clone of this repository with --reviewed-ref fetched")
    x.add_argument("--kubeconfig", required=True, help="namespace-scoped bucket-policy identity")
    x.add_argument("--namespace", required=True)
    x.add_argument("--object-endpoint", required=True)
    x.add_argument("--probe-key", required=True)
    x.add_argument("--bucket", default=APP_BUCKET)
    x.add_argument("--object-namespace", default="")
    x.add_argument("--auth-secret", default=AUTH_SECRET)
    x.add_argument("--object-workload", default=WORKLOAD)
    x.add_argument("--object-container", default=CONTAINER)
    x.add_argument("--receipts", required=True, help="durable directory holding one record per application_id")
    x.add_argument("--plan-id", default="", help="migration plan the recheck belongs to, when there is one")
    x.add_argument("--timeout", type=int, default=300, help="rollout wait in seconds")
    x.set_defaults(func=cmd_desired_exposure)
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
    if getattr(args, "object_endpoint", "") and not args.probe_key:
        parser.error("--probe-key is required with --object-endpoint")
    if args.command == "run":
        try:
            _policy_target(args)
        except ValueError as exc:
            parser.error(str(exc))
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
