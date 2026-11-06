"""Operator side of the initial AI proposal (#503 Job carrier), relayed to T10 inside the platform.

    uv run --directory services/platform-events python ../../operations/initial-proposal/run.py \
        start|collect|fence --plan-id P --version 3 --ref ref.json --principal <T10 grant principal> \
        --state-dir <operator ledger> --context <platform kubeconfig context> [--legacy-intent old.json]

T10 work runs in the platform through `--relay` (default: kubectl exec into the workflow host,
which holds the database credentials and the relay's fixed principals); Job creation and receipt opening stay with
this operator (personal kubeconfig, operator ledger). The runtime source and image come from the
GitOps Reader pin, so the intent can only run the image built from its own source.

start    fields -> recipient key -> T10 claim -> one Job (pending until it finishes)
collect  read the Job receipt -> T10 rechecks and stores it -> prints the values T15 needs
fence    prove the Actions-era request was never received, fence it here and in T10 (#543); uses
         --legacy-intent and --legacy-principal/--legacy-ref of the fenced admission
admit    after the fence, admit the same input for --principal (the Job identity); writes --ref
Exit: 0 done, 10 pending (the Job is still running: run the same command again later; it never
resubmits), 3 hold (T10 or the carrier stopped it: a person reads the reason), 1 failed.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from bravo_platform_events import initial_proposal_job as job
from bravo_platform_events.initial_proposal import InitialIntent

# The workflow host Pod holds the DB credentials and the relay's fixed principals (/workflow/relay.json).
RELAY = ["kubectl", "--context", "{context}", "-n", "platform-state", "exec", "-i", "deploy/t10-workflow-host",
         "-c", "host", "--", "python", "-m", "bravo_platform_events.initial_proposal_relay"]


def relay_with(argv):
    def call(request):
        result = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True, timeout=120,
                                check=False)
        answer = json.loads((result.stdout.strip().splitlines() or ["{}"])[-1])
        if answer.get("status") != "PASS":
            raise RuntimeError(f"t10_{answer.get('status', 'FAIL').lower()}:{answer.get('code') or answer.get('reason')}")
        return answer
    return call


def intent_path(state_dir, plan_id, version):
    """The one intent this operator started for a plan version (the claim forbids recomputing it)."""
    return Path(state_dir) / "intents" / f"{plan_id}-{version}.json"


def run(args, *, relay, client):
    ref = json.loads(Path(args.ref).read_text()) if Path(args.ref).exists() else {}
    base = {"plan_id": args.plan_id, "version": args.version, "ref": ref,
            "principal": args.principal, "runtime_source_sha": client.source_revision}
    if args.command == "fence":
        legacy = InitialIntent.model_validate_json(Path(args.legacy_intent).read_text())
        proof = client.fence_unreceived(legacy)
        old = base | {"principal": args.legacy_principal, "ref": json.loads(Path(args.legacy_ref).read_text()),
                      "runtime_source_sha": legacy.runtime_source_sha}
        return {"fenced": relay(old | {"op": "fence", "proof": proof})["proof"]["request_id"]}
    if args.command == "admit":
        ref = relay(base | {"op": "admit", "tenant_id": args.tenant_id, "task_id": args.task_id,
                            "expires_at": args.expires_at})["ref"]
        Path(args.ref).write_text(json.dumps(ref))
        return {"ref": ref}
    path = intent_path(args.state_dir, args.plan_id, args.version)
    if args.command == "start":
        if path.exists():  # already claimed by this operator: never recompute, only read
            intent = InitialIntent.model_validate_json(path.read_text())
        else:
            fields = relay(base | {"op": "fields"})["fields"]
            intent = InitialIntent(**fields, recipient_public_key=client.recipient(fields["request_id"]))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(intent.model_dump_json())
        relay(base | {"op": "claim", "recipient_public_key": intent.recipient_public_key})
        client.submit(intent)  # idempotent per request id; raises ExecutionPending while the Job runs
        return {"request_id": intent.request_id}
    intent = InitialIntent.model_validate_json(path.read_text())
    receipt = client.read(intent)
    stored = relay(base | {"op": "store", "envelope": receipt})["result"]
    return {"request_id": intent.request_id, "runtime_source_sha": stored["runtime_source_sha"],
            "proposal_digest": stored["proposal_digest"]}


def expand(argv):
    """`--from FILE` (used by scripts/happy_path.py): the same options as one private JSON file."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--from" not in argv:
        return argv
    i = argv.index("--from")
    options = json.loads(Path(argv[i + 1]).read_text())
    extra = []
    for key, value in options.items():
        extra += [f"--{key.replace('_', '-')}", *(value if isinstance(value, list) else [str(value)])]
    return argv[:i] + extra + argv[i + 2:]


def main(argv=None):
    argv = expand(argv)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["start", "collect", "fence", "admit"])
    p.add_argument("--plan-id", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--ref", required=True, help="JSON file: {input_ref, input_digest} of the admitted input")
    p.add_argument("--principal", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--gitops-image", type=Path, default=Path(__file__).resolve().parents[2]
                   / "infra/gitops/environments/development/product-reader-image.json")
    p.add_argument("--legacy-intent")
    p.add_argument("--legacy-principal")
    p.add_argument("--legacy-ref")
    p.add_argument("--tenant-id")
    p.add_argument("--task-id")
    p.add_argument("--expires-at")
    p.add_argument("--relay", nargs="+", help="argv reaching initial_proposal_relay in the platform")
    args = p.parse_args(argv)
    image, revision = job.gitops_image(args.gitops_image)
    client = job.JobInitialProposal(args.state_dir, image=image, source_revision=revision,
                                    kubectl=job.OperatorKubectl(args.context))
    relay = relay_with([part.format(context=args.context) for part in (args.relay or RELAY)])
    try:
        print(json.dumps({"status": "DONE", **run(args, relay=relay, client=client)}))
        return 0
    except ExecutionPending as pending:
        print(json.dumps({"status": "PENDING", "reason": str(pending)}))
        return 10
    except (ExecutionUnknown, RuntimeError, ValueError) as error:
        hold = isinstance(error, ExecutionUnknown) or "hold" in str(error)
        print(json.dumps({"status": "HOLD" if hold else "FAILED", "reason": str(error)[:200]}))
        return 3 if hold else 1


if __name__ == "__main__":
    sys.exit(main())
