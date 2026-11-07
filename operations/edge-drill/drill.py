"""Operator helper for the approved Edge drill (ADR-0039). Procedure: docs/operations/t17-edge-drill.md.

Runs on the operator's machine with the operator's own AWS SSO session; it never prints a proof or a token.

    cd services/platform-events
    uv run --extra sso-registration python ../../operations/edge-drill/drill.py \
        --kubeconfig PLATFORM_KUBECONFIG --cluster-uid KUBE_SYSTEM_UID approve --requested-by SUBJECT drill-a.json [drill-b.json]
    ... drill.py --kubeconfig ... --cluster-uid ... run drill-a.json register|sequence|watch|restore

`approve` asks the approval host for each spec digest (`bravo_edge_control.drill digest`), then records one T10 infra
approval per spec as the human. It grants nothing: permissions come from the administration path only. The requester
must be another registered person (`operator_verifier`), never the approver. The approval id and the approver are
written back into the local spec.
`run` refuses when the person running is the approver, copies the spec to the approval host and streams a fresh proof
every 20 s into the drill (60 s lifetime). If `sequence`/`watch` is interrupted (Ctrl-C, SIGTERM, a lost proof feed) or
ends non-zero, it stops the remote drill and runs `restore` (retrying while the drill lock is held) so the weight goes
back to 0%. Do not run it under a tool time limit shorter than the sequence.
Output lines are receipts and judgements; they are also appended to `<spec>.<action>.jsonl`.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

NAMESPACE, SELECTOR, CONTAINER = "platform-state", "app.kubernetes.io/name=t15-rehearsal-host", "host"
REMOTE_DIR = "/records/edge"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MOVES_WEIGHT = ("sequence", "watch")

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
people = people if isinstance(people, list) else [people]
verifiers = [SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE})) for o in people]
human = OperatorSsoVerifier(verifiers).verify(data["proof"])
if human is None:
    raise SystemExit("operator proof not verified")
if data["requested_by"] not in {v.subject_id for v in verifiers} or data["requested_by"] == human.subject_id:
    raise SystemExit("requester must be another registered person")
store = PostgresPlanStore(runtime_connection())
access = OperatorAccess(store)
start = datetime.fromtimestamp(human.issued_at, UTC)
out = {}
service = InfraApprovalService(store, check_approve=access.requires("approve_infra"), check_apply=access.requires("apply_infra"),
    check_access=access.check_read, check_approver=access.requires_subject("approve_infra"))
for run_id, digest in data["digests"].items():
    r = service.approve(human, data["plan_id"], binding=dict(infra_plan_id=run_id, plan_digest=digest,
        requested_by=data["requested_by"]), expires_at=(start + timedelta(hours=2)).isoformat(),
        command_key="edge-drill-approval-" + run_id)
    out[run_id] = dict(approval_id=r["approval_id"], expires_at=r["expires_at"], approved_by=human.subject_id)
print(json.dumps(out))
'''

WHOAMI = r'''
import json, sys
from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
from bravo_platform_events.registration_sso import SsoRegistrationVerifier
people = json.load(open("/rehearsal/host.json"))["operator_verifier"]
people = people if isinstance(people, list) else [people]
human = OperatorSsoVerifier([SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE})) for o in people]).verify(sys.stdin.read().strip())
print(json.dumps({"subject_id": human.subject_id if human else None}))
'''



# The image has no procps: find the drill for this config in /proc and send it SIGTERM.
STOP = r'''
import os, signal, sys
remote = sys.stdin.read().strip()
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    if b"bravo_edge_control.drill" in argv and remote.encode() in argv and int(pid) != os.getpid():
        os.kill(int(pid), signal.SIGTERM)
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
    if not RUN_ID.fullmatch(str(spec.get("run_id", ""))):
        raise SystemExit("run_id must match " + RUN_ID.pattern)
    remote = f"{REMOTE_DIR}/drill-{spec['run_id']}.json"
    kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "sh", "-c", f"umask 077; cat > {shlex.quote(remote)}"],
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
    payload = {"proof": proof(), "digests": digests, "plan_id": plans.pop(), "requested_by": args.requested_by}
    out = json.loads(kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "python3", "-c", APPROVE],
                             kubeconfig=args.kubeconfig, stdin=json.dumps(payload)))
    for run_id, (path, spec) in specs.items():
        spec["approval_id"], spec["approved_by"] = out[run_id]["approval_id"], out[run_id]["approved_by"]
        Path(path).write_text(json.dumps(spec, indent=2))
        os.chmod(path, 0o600)
    print(json.dumps({"digests": digests, **out}, indent=1))


def stream(args, pod, remote, action, *, stop=None):
    """Run one drill action, feeding a fresh proof every 20 s. Returns (exit code, interrupted)."""
    child = subprocess.Popen(["kubectl", "--kubeconfig", args.kubeconfig, "-n", NAMESPACE, "exec", "-i", pod, "-c",
                              CONTAINER, "--", "python", "-m", "bravo_edge_control.drill", "--config", remote, action],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    lost = threading.Event()

    def feed():  # the operator stays present while the drill runs; a lost feed counts as an interruption
        try:
            while child.poll() is None:
                child.stdin.write(proof() + "\n")
                child.stdin.flush()
                time.sleep(20)
        except (BrokenPipeError, ValueError):
            pass
        except Exception as error:  # noqa: BLE001 - SSO expiry etc.: report and let the caller restore
            print(json.dumps({"warning": "proof_feed_failed", "error": type(error).__name__}), file=sys.stderr)
            lost.set()

    threading.Thread(target=feed, daemon=True).start()
    log = Path(f"{args.spec}.{action}.jsonl")
    with log.open("a") as out:
        for line in child.stdout:
            out.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            if lost.is_set() or (stop is not None and stop.is_set()):
                break
    interrupted = lost.is_set() or (stop is not None and stop.is_set())
    if interrupted:
        child.terminate()
    return child.wait(), interrupted


def stop_remote(args, pod, remote):
    """`kubectl exec` without a TTY does not pass signals: end the remote drill for this spec by its config path."""
    kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "python3", "-c", STOP],
            kubeconfig=args.kubeconfig, stdin=remote, check=False)


def restore(args, pod, remote, attempts=10):
    """Back to 0%: retry while the stopped drill still holds the journal lock (shift_runtime_already_running)."""
    for attempt in range(attempts):
        code, _ = stream(args, pod, remote, "restore")
        if code == 0:
            return True
        time.sleep(min(5 * (attempt + 1), 30))
    return False


def run(args):
    pod = host(args)
    spec, remote = put(args, pod, args.spec)
    if spec.get("approval_id") in (None, "", "PENDING"):
        raise SystemExit("approve the spec first")
    me = json.loads(kubectl(["-n", NAMESPACE, "exec", "-i", pod, "-c", CONTAINER, "--", "python3", "-c", WHOAMI],
                            kubeconfig=args.kubeconfig, stdin=proof()))["subject_id"]
    if me is None or me == spec.get("approved_by"):
        raise SystemExit("the approver cannot run the drill: another registered person runs it")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    code, interrupted = stream(args, pod, remote, args.action, stop=stop)
    if args.action in MOVES_WEIGHT and (interrupted or code != 0):
        stop_remote(args, pod, remote)
        restored = restore(args, pod, remote)
        print(json.dumps({"interrupted": interrupted, "exit": code, "restored_to_zero": restored}))
        raise SystemExit(1 if not restored else 130 if interrupted else code)
    raise SystemExit(code)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, help="platform cluster (tailnet operator proxy)")
    parser.add_argument("--cluster-uid", required=True, help="expected platform kube-system UID")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("approve")
    a.add_argument("specs", nargs="+", help="drill spec JSON files (one plan)")
    a.add_argument("--requested-by", required=True, help="subject id of another registered person (not the approver)")
    r = sub.add_parser("run")
    r.add_argument("spec")
    r.add_argument("action", choices=("register", "sequence", "watch", "restore"))
    args = parser.parse_args(argv)
    (approve if args.command == "approve" else run)(args)


if __name__ == "__main__":
    main()
