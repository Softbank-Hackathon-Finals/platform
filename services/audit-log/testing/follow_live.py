"""Live check: real local T10 event API -> ``bravo-audit follow`` -> the T24 Object Lock bucket.

A fresh plan is registered in a real T10 ``PlanStore`` (local SQLite) and served by the real
``create_app``. ``follow --once`` records what exists, more events are produced, ``follow --once``
resumes from the bucket, a third run finds nothing new, and the existing ``verify`` command
checks the stored chain with the reader identity.

The bucket, profiles and output directory are arguments; nothing private is in this file. The
output directory receives raw outputs (receipts carry version IDs) and must be private:

    uv run --locked --extra aws python testing/follow_live.py --sink s3://<bucket>/audit \\
      --writer-profile <writer> --reader-profile <reader> --out <private dir>
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from t10_events_server import T10Server, t10


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sink", required=True)
    parser.add_argument("--writer-profile", required=True)
    parser.add_argument("--reader-profile", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    _api, _store, plans = t10()

    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    plan_id, run_id = f"plan-t24-follow-{stamp}", f"run-t24-follow-{stamp}"
    token = secrets.token_urlsafe(32)
    token_file = out / "t10.token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    store = plans.PlanStore(out / "t10.sqlite")
    spec = plans.PlanSpec(plan_id, "tenant-t24", "workload-t24", "env-source", "env-target",
                          "a" * 64, "b" * 64)
    produced: list[dict] = []

    def append(event_type: str, data: dict, key: str) -> None:
        produced.append(store.append(plan_id=plan_id, run_id=run_id, event_type=event_type, data=data,
                                     occurred_at=now(), producer_key=key))

    # Round 1: registration, the guarded COLLECT -> ANALYZE transition, a gate result.
    store.register_plan(spec, run_id=run_id, occurred_at=now())
    store.complete_collection(plans.CollectionProof(plan_id, spec.plan_digest, spec.collection_scope_digest,
                                                    "evidence-collection", True),
                              expected_version=1, command_key="collect-1", run_id=run_id, occurred_at=now())
    append("gate.completed", {"gate_id": "gate-initial", "evidence_ref": "evidence-gate-initial",
                              "decision": "PASS", "context_digest": "c" * 64}, "gate-initial")

    steps: dict[str, dict] = {}
    outputs: list[bytes] = []

    def run(name: str, argv: list[str], profile: str) -> dict:
        env = dict(os.environ, AWS_PROFILE=profile)
        started = now()
        proc = subprocess.run([sys.executable, "-m", "bravo_audit.cli", *argv], env=env,
                              capture_output=True, timeout=600)
        (out / f"{name}.stdout").write_bytes(proc.stdout)
        (out / f"{name}.stderr").write_bytes(proc.stderr)
        outputs.extend([proc.stdout, proc.stderr])
        lines = proc.stdout.decode().strip().splitlines()
        result = {"observed_at_start": started, "observed_at_end": now(), "exit": proc.returncode,
                  "summary": json.loads(lines[-1]) if lines else None}
        steps[name] = result
        return result

    with T10Server(store, token, poll_seconds=0.2) as server:
        def follow(name: str) -> dict:
            return run(name, ["follow", "--base-url", server.base_url, "--plan", plan_id, "--sink", args.sink,
                              "--once", "--auth-token-file", str(token_file),
                              "--receipts", str(out / f"{name}.receipts.jsonl")], args.writer_profile)

        follow("follow_1_initial")
        # Round 2, after the first follower exited: approval request and execution progress.
        approval = {"approval_id": "approval-cutover-1", "approval_kind": "cutover",
                    "binding": {"plan_id": plan_id, "plan_digest": spec.plan_digest,
                                "diff_hash": "sha256:" + "d" * 64, "image_digest": "sha256:" + "e" * 64,
                                "target_environment_id": "env-target", "expires_at": "2026-11-07T00:00:00Z",
                                "gate_id": "gate-initial", "context_digest": "c" * 64,
                                "gate_phase": "initial", "application_id": None}}
        append("approval.requested", approval, "approval-cutover-1")
        append("execution.updated", {"execution_id": "execution-1", "step": "pause-writes",
                                     "status": "started"}, "execution-1-start")
        append("execution.updated", {"execution_id": "execution-1", "step": "pause-writes",
                                     "status": "succeeded", "elapsed_ms": 1200}, "execution-1-done")
        follow("follow_2_resume")
        follow("follow_3_nothing_new")
        cursors = list(server.cursors)
        presented_ok = all(p == f"Bearer {token}" for p in server.presented)

    root = args.sink.rstrip("/") if args.sink.startswith("s3://") else f"{args.sink.rstrip('/')}/audit"
    run("verify_reader", ["verify", f"{root}/{plan_id}/{run_id}"], args.reader_profile)
    receipts = []
    for name in ("follow_1_initial", "follow_2_resume", "follow_3_nothing_new"):
        path = out / f"{name}.receipts.jsonl"
        receipts += [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    results = {
        "plan_id": plan_id, "run_id": run_id,
        "t10_events": [{"sequence": e["sequence"], "event_type": e["event_type"]} for e in store.read_after(plan_id, None)],
        "steps": steps,
        "last_event_id_sent": cursors,
        "auth_header_presented_on_every_request": presented_ok,
        "credential_in_any_output": any(token.encode() in o for o in outputs),
        "receipts": receipts,
    }
    (out / "results.raw.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    for path in out.iterdir():
        path.chmod(0o600 if path.is_file() else 0o700)
    print(json.dumps({k: v for k, v in results.items() if k != "receipts"}, indent=2, ensure_ascii=False))
    ok = all(s["exit"] == 0 for s in steps.values()) and not results["credential_in_any_output"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
