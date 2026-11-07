"""Register, revoke or preflight security reviews through the protected approval host."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def relay(context, request):
    command = ["kubectl", "--context", context, "-n", "platform-state", "exec", "-i",
               "deploy/t15-rehearsal-host", "-c", "host", "--", "python", "-m",
               "bravo_platform_events.security_relay"]
    result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True,
                            timeout=120, check=False)
    lines = result.stdout.strip().splitlines()
    if result.returncode:
        return {"status": "FAIL", "reason": "security_relay_unavailable"}
    return json.loads(lines[-1]) if lines else {"status": "FAIL", "reason": "no_output"}


def require_pass(answer):
    if answer.get("status") != "PASS":
        print(json.dumps(answer, indent=2, sort_keys=True))
        raise SystemExit(10 if answer.get("status") == "HOLD" else 1)
    return answer


def warm(context):
    for deployment in ("security-review", "t15-rehearsal-host"):
        result = subprocess.run(["kubectl", "--context", context, "-n", "platform-state", "rollout",
                                 "status", "deployment/" + deployment, "--timeout=120s"],
                                capture_output=True, text=True, timeout=130, check=False)
        if result.returncode:
            print(json.dumps({"status": "FAIL", "reason": "deployment_not_ready",
                              "deployment": deployment}, sort_keys=True))
            raise SystemExit(1)


def registration(plan_id, document, expected_revision, command_key):
    return {"op": "register", "plan_id": plan_id, "document": json.loads(document.read_text()),
            "expected_revision": expected_revision, "command_key": command_key}


def prepare(args, documents):
    warm(args.context)
    registered = []
    for name, path, revision in documents:
        command_key = "demo-" + hashlib.sha256(f"{args.plan_id}:{name}".encode()).hexdigest()
        answer = require_pass(relay(args.context, registration(args.plan_id, path, revision,
                                                               command_key)))
        registered.append(answer["registration"])
    stage = "change" if args.command == "prepare-change" else "cutover"
    checked = require_pass(relay(args.context, {"op": "probe", "plan_id": args.plan_id,
                                                "stage": stage}))
    return {"status": "PASS", "stage": stage, "warmed": ["security-review", "t15-rehearsal-host"],
            "registered": registered, "probe": checked["probe"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "revoke", "probe", "prepare-change", "prepare-cutover"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--command-key")
    parser.add_argument("--document", type=Path)
    parser.add_argument("--kind", choices=("patch", "migration"))
    parser.add_argument("--phase", choices=("preflight", "postflight"))
    parser.add_argument("--stage", choices=("change", "cutover"))
    parser.add_argument("--patch-preflight", type=Path)
    parser.add_argument("--patch-postflight", type=Path)
    parser.add_argument("--migration-preflight", type=Path)
    parser.add_argument("--patch-revision", type=int, default=0)
    parser.add_argument("--migration-revision", type=int, default=0)
    args = parser.parse_args()

    if args.command == "prepare-change":
        if args.patch_preflight is None:
            parser.error("prepare-change requires --patch-preflight")
        answer = prepare(args, (("patch-preflight", args.patch_preflight, args.patch_revision),))
    elif args.command == "prepare-cutover":
        if args.patch_postflight is None or args.migration_preflight is None:
            parser.error("prepare-cutover requires --patch-postflight and --migration-preflight")
        answer = prepare(args, (("patch-postflight", args.patch_postflight, args.patch_revision),
                                ("migration-preflight", args.migration_preflight, args.migration_revision)))
    elif args.command == "probe":
        if args.stage is None:
            parser.error("probe requires --stage")
        warm(args.context)
        answer = require_pass(relay(args.context, {"op": "probe", "plan_id": args.plan_id,
                                                   "stage": args.stage}))
    elif args.command == "register":
        if args.document is None or args.expected_revision is None or args.command_key is None:
            parser.error("register requires --document, --expected-revision and --command-key")
        answer = require_pass(relay(args.context, registration(args.plan_id, args.document,
            args.expected_revision, args.command_key)))
    else:
        if (args.kind is None or args.phase is None or args.expected_revision is None
                or args.command_key is None):
            parser.error("revoke requires --kind, --phase, --expected-revision and --command-key")
        answer = require_pass(relay(args.context, {"op": "revoke", "plan_id": args.plan_id,
            "expected_revision": args.expected_revision, "command_key": args.command_key,
            "kind": args.kind, "phase": args.phase}))
    print(json.dumps(answer, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
