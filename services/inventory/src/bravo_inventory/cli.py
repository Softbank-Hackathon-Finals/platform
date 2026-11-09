"""bravo-inventory: scan a registered app and write the inventory document.

Trusted-server library. Credentials for live AWS tracing come from the platform's collector
role (T7 read role); nothing is written to the cloud and the call list is in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .deploy_trace import FileAdapter
from .report import build_inventory


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravo-inventory", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="repo + deployment exports → inventory.json")
    s.add_argument("--app-id", required=True)
    s.add_argument("--repo", required=True, type=Path)
    s.add_argument("--repo-ref", help="registered repo reference written to app.repo, e.g. org/demo-chat (default: directory name; the local path is never written)")
    s.add_argument("--app-path", default=".")
    s.add_argument("--source-env", required=True, help="env id of the source, e.g. aws-seoul")
    s.add_argument("--source-kind", default="aws", choices=["aws", "gcp", "onprem"])
    s.add_argument("--namespace", required=True)
    s.add_argument("--exports", type=Path, help="directory with deployments.json · serviceaccounts.json · clusters.json · aws.json")
    s.add_argument("--aws-live", action="store_true", help="also trace IAM/S3 live with the read-only allowlist (needs extra 'aws')")
    s.add_argument("--profile", action="append", default=[], type=Path, help="candidate target profile JSON (repeatable)")
    s.add_argument("--target-exports", action="append", default=[], help="env_id=DIR with target exports for capability observation")
    s.add_argument("--source-sha")
    s.add_argument("--output", type=Path, required=True)
    args = p.parse_args(argv)

    adapter = None
    if args.exports:
        if args.aws_live:
            from .deploy_trace import Boto3Adapter

            adapter = Boto3Adapter(args.exports)
        else:
            adapter = FileAdapter(args.exports)
    exports = {}
    for spec in args.target_exports:
        env_id, _, d = spec.partition("=")
        exports[env_id] = Path(d)
    doc = build_inventory(
        app_id=args.app_id, repo=args.repo, repo_ref=args.repo_ref, app_path=args.app_path,
        source_env={"env_id": args.source_env, "env_kind": args.source_kind, "namespace": args.namespace},
        adapter=adapter, profiles=args.profile, exports=exports, source_sha=args.source_sha,
    )
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = doc["summary"]
    print(f"items={len(doc['items'])} grades={s['grade_counts']} agent_needed={s['agent_needed']} deploy_only={s['deploy_only']} digest={doc['digest'][:19]}…")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
