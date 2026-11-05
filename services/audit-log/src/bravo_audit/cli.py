"""bravo-audit: append upstream events to a run ledger, verify a stored chain, probe deletion denial."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import from_agent_tool_event, from_platform_event
from .denial import probe_denial
from .ledger import RunLedger, chain_digest, verify_chain
from .record import UnsafePayload
from .sink import FileSink, ObjectLockSink, UnverifiedWrite


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sink(target: str, retention_days: int):
    if target.startswith("s3://"):
        import boto3  # optional extra; the live path is only exercised with cloud access

        bucket, _, prefix = target[5:].partition("/")
        return ObjectLockSink(boto3.client("s3"), bucket, retention_days=retention_days, prefix=prefix or "audit")
    return FileSink(Path(target))


def cmd_append(args) -> int:
    ledger = None
    receipts = []
    sink = _sink(args.sink, args.retention_days)
    for event in _load_jsonl(Path(args.input)):
        try:
            if args.source == "platform.events":
                plan_id, run_id, source, kind, payload = from_platform_event(event)
            else:
                plan_id, run_id, source, kind, payload = from_agent_tool_event(event, args.plan)
        except ValueError as exc:
            print(f"rejected event: {exc}", file=sys.stderr)
            return 2
        if args.plan and plan_id != args.plan or args.run and run_id != args.run:
            print("event does not belong to the requested plan/run", file=sys.stderr)
            return 2
        if ledger is None:
            existing = _load_jsonl(Path(args.resume_from)) if args.resume_from else []
            ledger = RunLedger.resume(plan_id, run_id, existing)
        try:
            record = ledger.append(source, kind, payload)
            receipts.append(sink.put(record).as_dict())
        except (UnsafePayload, UnverifiedWrite, FileExistsError) as exc:
            print(f"stopped before record {ledger.next_sequence}: {type(exc).__name__}", file=sys.stderr)
            return 3
    out = {"plan_id": ledger.plan_id if ledger else args.plan, "run_id": ledger.run_id if ledger else args.run,
           "records": len(receipts), "chain_digest": chain_digest([r.as_dict() for r in ledger.records]) if ledger else None,
           "receipts": receipts}
    Path(args.receipts).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "receipts"}, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    files = sorted(Path(args.directory).glob("*.json"))
    records = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    ok, reason = verify_chain(records)
    print(json.dumps({"records": len(records), "ok": ok, "reason": reason, "chain_digest": chain_digest(records) if ok else None}))
    return 0 if ok else 1


def cmd_probe(args) -> int:
    import boto3

    result = probe_denial(boto3.client("s3"), args.bucket, args.key, args.version_id)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"pass": result["pass"], "retention": result["retention"]}))
    return 0 if result["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bravo-audit")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("append", help="append upstream events (JSON Lines) to a run ledger and a sink")
    a.add_argument("--source", choices=["platform.events", "agent.tools"], required=True)
    a.add_argument("--input", required=True)
    a.add_argument("--sink", required=True, help="directory or s3://bucket[/prefix]")
    a.add_argument("--receipts", required=True)
    a.add_argument("--plan", default="")
    a.add_argument("--run", default="")
    a.add_argument("--resume-from", default="", help="JSON Lines of already stored records of this run")
    a.add_argument("--retention-days", type=int, default=7)
    a.set_defaults(func=cmd_append)
    v = sub.add_parser("verify", help="verify a stored run directory (audit/<plan>/<run>)")
    v.add_argument("directory")
    v.set_defaults(func=cmd_verify)
    p = sub.add_parser("probe-denial", help="attempt deletion/alteration of a retained object; expect denial")
    p.add_argument("--bucket", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--version-id", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_probe)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
