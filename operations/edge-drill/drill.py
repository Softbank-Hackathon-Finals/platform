"""Operator helper for the approved Edge drill (ADR-0039). Procedure: docs/operations/t17-edge-drill.md.

Runs on the operator's machine with the operator's own AWS SSO session; it never prints a proof or a token.

    cd services/platform-events
    uv run --extra sso-registration python ../../operations/edge-drill/drill.py \
        --kubeconfig PLATFORM_KUBECONFIG --cluster-uid KUBE_SYSTEM_UID approve [--grant-hours 3] drill-a.json [drill-b.json]
    ... drill.py --kubeconfig ... --cluster-uid ... run drill-a.json register|sequence|watch|restore

`approve` asks the approval host for each spec digest (`bravo_edge_control.drill digest`), then, as the human,
optionally grants itself read/approve_infra/apply_infra on the plan and records one T10 infra approval per spec
(requested_by stays `--requested-by`, never the approver). The approval id is written back into the local spec.
`run` copies the spec to the approval host and streams a fresh proof every 20 s into the drill (60 s lifetime).
Output lines are receipts and judgements; they are also appended to `<spec>.<action>.jsonl`.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

NAMESPACE, SELECTOR, CONTAINER = "platform-state", "app.kubernetes.io/name=t15-rehearsal-host", "host"
REMOTE_DIR = "/records/edge"

APPROVE = r'''
import json, sys
from datetime import UTC, datetime, timedelta
from bravo_platform_events.window_server import runtime_connection
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
from bravo_platform_events.registration_sso import SsoRegistrationVerifier
from bravo_platform_events.infra_approval import InfraApprovalService
data = json.load(sys.stdin)
people = json.load(open("/rehearsal/host.json"))["operator_verifier"]
human = OperatorSsoVerifier([SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE})) for o in people]).verify(data["proof"])
if human is None:
    raise SystemExit("operator proof not verified")
store = PostgresPlanStore(runtime_connection())
access = OperatorAccess(store)
start = datetime.fromtimestamp(human.issued_at, UTC)
out = {}
if data["grant_hours"]:
    with store._transaction() as c:
        old = access._grant(c, data["plan_id"], human.subject_id)
    grant = access.set_permissions(data["plan_id"], human, permissions=["read", "approve_infra", "apply_infra"],
        valid_after=start.isoformat(), expires_at=(start + timedelta(hours=data["grant_hours"])).isoformat(),
        expected_revision=old["revision"] if old else 0, administration_ref=data["administration_ref"],
        command_key="edge-drill-grant-" + data["key"])
    out["grant"] = dict(revision=grant["revision"], expires_at=grant["expires_at"])
service = InfraApprovalService(store, check_approve=access.requires("approve_infra"), check_apply=access.requires("apply_infra"),
    check_access=access.check_read, check_approver=access.requires_subject("approve_infra"))
for run_id, digest in data["digests"].items():
    r = service.approve(human, data["plan_id"], binding=dict(infra_plan_id=run_id, plan_digest=digest,
        requested_by=data["requested_by"]), expires_at=(start + timedelta(hours=2)).isoformat(),
        command_key="edge-drill-approval-" + run_id)
    out[run_id] = dict(approval_id=r["approval_id"], expires_at=r["expires_at"])
print(json.dumps(out))
'''


def kubectl(args, *, kubeconfig, stdin=None, check=True):
    result = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args], input=stdin, capture_output=True,
                            text=True, check=False)
    if check and result.returncode:
        raise SystemExit(f"kubectl {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result.stdout


def proof():
    from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE
    from bravo_platform_events.registration_sso import make_registration_token

    return make_registration_token(profile_name=os.environ.get("AWS_PROFILE", "bravo-operator"),
                                   region=os.environ.get("AWS_REGION", "ap-northeast-2"), audience=OPERATOR_SSO_AUDIENCE)


def host(args):
    if kubectl(["get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"], kubeconfig=args.kubeconfig) != args.cluster_uid:
        raise SystemExit("cluster identity mismatch: stop")
    return kubectl(["-n", NAMESPACE, "get", "pod", "-l", SELECTOR, "-o", "jsonpath={.items[0].metadata.name}"],
                   kubeconfig=args.kubeconfig)


def put(args, pod, spec_path):
    spec = json.loads(Path(spec_path).read_text())
    remote = f"{REMOTE_DIR}/drill-{spec['run_id']}.json"
    kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "sh", "-c", f"umask 077; cat > {remote}"],
            kubeconfig=args.kubeconfig, stdin=json.dumps(spec))
    return spec, remote


def drill(args, pod, remote, action, **kw):
    return kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "python", "-m",
                    "bravo_edge_control.drill", "--config", remote, action], kubeconfig=args.kubeconfig, **kw)


def approve(args):
    pod = host(args)
    specs, digests = {}, {}
    for path in args.specs:
        spec, remote = put(args, pod, path)
        digests[spec["run_id"]] = json.loads(drill(args, pod, remote, "digest"))["plan_digest"]
        specs[spec["run_id"]] = (path, spec)
    plans = {spec["plan_id"] for _, spec in specs.values()}
    if len(plans) != 1:
        raise SystemExit("one plan per approval call")
    payload = {"proof": proof(), "digests": digests, "plan_id": plans.pop(), "grant_hours": args.grant_hours,
               "requested_by": args.requested_by, "administration_ref": args.administration_ref,
               "key": "-".join(sorted(digests))[:120]}
    out = json.loads(kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "python3", "-c", APPROVE],
                             kubeconfig=args.kubeconfig, stdin=json.dumps(payload)))
    for run_id, (path, spec) in specs.items():
        spec["approval_id"] = out[run_id]["approval_id"]
        Path(path).write_text(json.dumps(spec, indent=2))
        os.chmod(path, 0o600)
    print(json.dumps({"digests": digests, **out}, indent=1))


def run(args):
    pod = host(args)
    spec, remote = put(args, pod, args.spec)
    if spec.get("approval_id") in (None, "", "PENDING"):
        raise SystemExit("approve the spec first")
    child = subprocess.Popen(["kubectl", "--kubeconfig", args.kubeconfig, "-n", NAMESPACE, "exec", "-i", pod, "-c",
                              CONTAINER, "--", "python", "-m", "bravo_edge_control.drill", "--config", remote, args.action],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def feed():  # the operator stays present: a fresh proof every 20 s while the drill runs
        try:
            while child.poll() is None:
                child.stdin.write(proof() + "\n")
                child.stdin.flush()
                time.sleep(20)
        except (BrokenPipeError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    log = Path(f"{args.spec}.{args.action}.jsonl")
    with log.open("a") as out:
        for line in child.stdout:
            out.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
    raise SystemExit(child.wait())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, help="platform cluster (tailnet operator proxy)")
    parser.add_argument("--cluster-uid", required=True, help="expected platform kube-system UID")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("approve")
    a.add_argument("specs", nargs="+", help="drill spec JSON files (one plan)")
    a.add_argument("--grant-hours", type=int, default=0, help="also grant read/approve_infra/apply_infra (0: keep)")
    a.add_argument("--requested-by", default="agent-edge-drill")
    a.add_argument("--administration-ref", default="operator-edge-drill-decision")
    r = sub.add_parser("run")
    r.add_argument("spec")
    r.add_argument("action", choices=("register", "sequence", "watch", "restore"))
    args = parser.parse_args(argv)
    (approve if args.command == "approve" else run)(args)


if __name__ == "__main__":
    main()
