"""bravo-audit: append upstream events to a run ledger, follow a T10 event stream, verify a stored
chain, probe deletion denial."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import expand_platform_event, from_agent_tool_event
from .denial import probe_denial
from .ledger import OriginConflict, RunLedger, chain_digest, verify_chain
from .record import UnsafePayload
from .sink import FileSink, ObjectLockSink, UnverifiedWrite
from .storage import StoreTampered, _load_s3_run, split_s3


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_records(target: str) -> list[dict]:
    """Stored records of one run: a local directory, a JSON Lines file, or s3://bucket/<prefix>/<plan>/<run>.
    The S3 path only lists and reads (the T24 reader role); objects are taken in key order, which is
    sequence order because keys are zero-padded."""
    if target.startswith("s3://"):
        import boto3

        bucket, _, prefix = target[5:].partition("/")
        return _load_s3_run(boto3.client("s3"), bucket, prefix.rstrip("/") + "/")
    path = Path(target)
    if path.is_dir():
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))]
    return _load_jsonl(path)


def _sink(target: str, retention_days: int):
    if target.startswith("s3://"):
        import boto3  # optional extra; the live path is only exercised with cloud access

        bucket, prefix = split_s3(target)
        return ObjectLockSink(boto3.client("s3"), bucket, retention_days=retention_days, prefix=prefix)
    return FileSink(Path(target))


def cmd_append(args) -> int:
    ledger = None
    receipts, absorbed = [], 0
    sink = _sink(args.sink, args.retention_days)
    # Every event is mapped and bound to one (plan, run) *before* anything is written: a batch
    # that mixes runs is rejected whole, so a record can never be attributed to another run.
    mapped = []
    for index, event in enumerate(_load_jsonl(Path(args.input))):
        try:
            if args.source == "platform.events":
                mapped.extend(expand_platform_event(event))
            else:
                mapped.append(from_agent_tool_event(event, args.plan, index))
        except ValueError as exc:
            print(f"rejected event {index}: {exc}", file=sys.stderr)
            return 2
    identities = {(m[0], m[1]) for m in mapped}
    if len(identities) > 1:
        print("input mixes more than one plan/run; one ledger per run", file=sys.stderr)
        return 2
    if mapped:
        plan_id, run_id = mapped[0][:2]
        if args.plan and plan_id != args.plan or args.run and run_id != args.run:
            print("events do not belong to the requested plan/run", file=sys.stderr)
            return 2
        try:
            existing = _load_records(args.resume_from) if args.resume_from else []
        except StoreTampered as exc:
            print(f"stored chain is not trustworthy: {exc}", file=sys.stderr)
            return 3
        ledger = RunLedger.resume(plan_id, run_id, existing)
    for _plan, _run, source, kind, origin_id, payload in mapped:
        try:
            record, created = ledger.append(source, kind, payload, origin_id)
            if not created:
                absorbed += 1
                continue
            receipts.append(sink.put(record).as_dict())
        except (UnsafePayload, UnverifiedWrite, FileExistsError, OriginConflict) as exc:
            print(f"stopped before record {ledger.next_sequence}: {type(exc).__name__}", file=sys.stderr)
            return 3
    out = {"plan_id": ledger.plan_id if ledger else args.plan, "run_id": ledger.run_id if ledger else args.run,
           "records": len(receipts), "absorbed_redeliveries": absorbed,
           "chain_digest": ledger.chain_digest() if ledger else None,
           "receipts": receipts}
    Path(args.receipts).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "receipts"}, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    try:
        records = _load_records(args.directory)
    except StoreTampered as exc:
        print(json.dumps({"records": None, "ok": False, "reason": str(exc), "chain_digest": None}))
        return 1
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
    a.add_argument("--resume-from", default="",
                   help="already stored records of this run: JSON Lines, a run directory or s3://bucket/<prefix>/<plan>/<run>")
    # Demo default: one day, so the platform account can be torn down the day after. Production
    # passes the regulated period explicitly.
    a.add_argument("--retention-days", type=int, default=1)
    a.set_defaults(func=cmd_append)
    v = sub.add_parser("verify", help="verify a stored run: directory or s3://bucket/<prefix>/<plan>/<run>")
    v.add_argument("directory", help="run directory, JSON Lines file or s3://bucket/<prefix>/<plan>/<run>")
    v.set_defaults(func=cmd_verify)
    p = sub.add_parser("probe-denial", help="attempt deletion/alteration of a retained object; expect denial")
    p.add_argument("--bucket", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--version-id", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_probe)
    from .follow import add_parser as add_follow

    add_follow(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
