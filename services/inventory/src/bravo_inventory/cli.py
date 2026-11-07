"""bravo-inventory: scan a registered app and write the inventory document.

Trusted-server library. Credentials for live tracing come from the platform's collector role
(T7 read role); nothing is written to the cloud and the call list is in the output.

``--source-kind`` selects the source adapters for the whole run (ADR-0034): the static rules
(``source_rules.STATIC_RULES``), the deploy adapter (``sources.DEPLOY_ADAPTERS``) and, through
the kind recorded on every finding and on the deploy graph, the reconcile step. A kind without
an adapter stops the run with ``UNSUPPORTED`` (exit 3); another kind's adapter is never used.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

from .report import build_inventory
from .source_rules import KINDS, Unsupported, static_rules_for
from .sources import deploy_adapter_for
from .static_scan import use_source_kind

EXIT_UNSUPPORTED = 3


def _build_kwargs(target_kind: str | None) -> dict:
    """``target_kind`` goes to ``build_inventory`` only when it accepts it (A4 catalog hub)."""
    if target_kind and "target_kind" in inspect.signature(build_inventory).parameters:
        return {"target_kind": target_kind}
    return {}


def _adapter(kind: str, exports: Path | None, live: bool):
    if exports is None:
        return None
    return deploy_adapter_for(kind, exports, live=live)


def _unsupported(u: Unsupported) -> int:
    print(u.reason, file=sys.stderr)
    return EXIT_UNSUPPORTED


def _live(args: argparse.Namespace) -> bool | None:
    """``--live`` (any kind) or the T16 ``--aws-live`` (aws only). ``None``: invalid combination."""
    if getattr(args, "aws_live", False) and args.source_kind != "aws":
        return None
    return bool(args.live or getattr(args, "aws_live", False))


def _scan(args: argparse.Namespace) -> int:
    rules = static_rules_for(args.source_kind)
    if isinstance(rules, Unsupported):
        return _unsupported(rules)
    live = _live(args)
    if live is None:
        print("--aws-live needs --source-kind aws (use --live)", file=sys.stderr)
        return 2
    adapter = _adapter(args.source_kind, args.exports, live)
    if isinstance(adapter, Unsupported):
        return _unsupported(adapter)
    exports = {}
    for spec in args.target_exports:
        env_id, _, d = spec.partition("=")
        exports[env_id] = Path(d)
    with use_source_kind(args.source_kind):
        doc = build_inventory(
            app_id=args.app_id, repo=args.repo, repo_ref=args.repo_ref, app_path=args.app_path,
            source_env={"env_id": args.source_env, "env_kind": args.source_kind, "namespace": args.namespace},
            adapter=adapter, profiles=args.profile, exports=exports, source_sha=args.source_sha,
            **_build_kwargs(args.target_kind),
        )
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2
    args.output.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = doc["summary"]
    print(f"items={len(doc['items'])} grades={s['grade_counts']} agent_needed={s['agent_needed']} deploy_only={s['deploy_only']} digest={doc['digest'][:19]}…")
    return 0


def _trace(args: argparse.Namespace) -> int:
    adapter = _adapter(args.source_kind, args.exports, args.live)
    if isinstance(adapter, Unsupported):
        return _unsupported(adapter)
    graph = adapter.trace(args.namespace).as_dict()
    text = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(text)
        return 0
    if args.output.exists():
        print(f"refusing to overwrite {args.output}", file=sys.stderr)
        return 2
    args.output.write_text(text, encoding="utf-8")
    print(f"resources={len(graph['resources'])} notes={len(graph['notes'])} api_calls={len(graph['api_calls'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravo-inventory", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="repo + deployment exports → inventory.json")
    s.add_argument("--app-id", required=True)
    s.add_argument("--repo", required=True, type=Path)
    s.add_argument("--repo-ref", help="registered repo reference written to app.repo, e.g. org/demo-chat (default: directory name; the local path is never written)")
    s.add_argument("--app-path", default=".")
    s.add_argument("--source-env", required=True, help="env id of the source, e.g. aws-seoul")
    s.add_argument("--source-kind", default="aws", choices=list(KINDS), help="selects static rules, deploy adapter and reconcile meaning")
    s.add_argument("--target-kind", choices=list(KINDS), help="target kind for grading when no --profile is given (needs the A4 catalog hub)")
    s.add_argument("--namespace", required=True)
    s.add_argument("--exports", type=Path, help="directory with Kubernetes exports and the kind's identity export (aws.json · gcp.json · openbao.json + s3.json)")
    s.add_argument("--live", action="store_true", help="also trace the source cloud live with the kind's read-only allowlist (aws: boto3, gcp: gcloud)")
    s.add_argument("--aws-live", action="store_true", help="T16 alias of --live for --source-kind aws")
    s.add_argument("--profile", action="append", default=[], type=Path, help="candidate target profile JSON (repeatable)")
    s.add_argument("--target-exports", action="append", default=[], help="env_id=DIR with target exports for capability observation")
    s.add_argument("--source-sha")
    s.add_argument("--output", type=Path, required=True)

    t = sub.add_parser("trace", help="deployment exports → deploy graph JSON (source.deploy_trace only)")
    t.add_argument("--source-kind", default="aws", choices=list(KINDS))
    t.add_argument("--namespace", required=True)
    t.add_argument("--exports", type=Path, required=True)
    t.add_argument("--live", action="store_true")
    t.add_argument("--output", type=Path, help="default: stdout")
    args = p.parse_args(argv)
    return _scan(args) if args.cmd == "scan" else _trace(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
