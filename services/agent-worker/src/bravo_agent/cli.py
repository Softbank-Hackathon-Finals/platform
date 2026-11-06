"""A one-shot platform process; no ambient developer settings or credentials."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from .contracts import FailureBundle, InventorySummary, Proposal, SnapshotManifest
from .coordinator import Coordinator
from .runtime import isolated_environment
from .tools import Snapshot


def bounded_json(path: Path, limit: int):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input too large")
    return json.loads(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print the proposal JSON Schema")
    analyze = commands.add_parser("analyze", help="Propose once; no execution or verification")
    for argument in ("bundle", "manifest", "snapshot", "output"):
        analyze.add_argument("--" + argument, type=Path, required=True)
    analyze.add_argument("--inventory", type=Path,
                         help="Optional T16 inventory document of the same source_sha; enables inventory.describe")
    analyze.add_argument("--model", required=True, help="Explicit provider model ID")
    analyze.add_argument("--provider", choices=("chatgpt-plan", "openai", "claude"), default="chatgpt-plan")
    analyze.add_argument("--exec-socket", type=Path,
                         help="Broker-created T42 exec socket for this run; absent means exec stays off")
    args = parser.parse_args()
    if args.command == "schema":
        print(json.dumps(Proposal.model_json_schema(), indent=2))
        return 0
    try:
        if args.output.exists():
            raise ValueError("output already exists")
        bundle = FailureBundle.model_validate(bounded_json(args.bundle, 16384))
        manifest = SnapshotManifest.model_validate(bounded_json(args.manifest, 32768))
        # Only the model-facing subset survives; a different revision is refused like any input error.
        inventory = (InventorySummary.from_document(bounded_json(args.inventory, 262144))
                     if args.inventory is not None else None)
        exec_options = {}
        if args.exec_socket is not None:
            from .exec_relay import SocketExecChannel

            # The broker opened this socket only for an input with exec_enabled exactly true.
            exec_options = {"exec_channel": SocketExecChannel(args.exec_socket), "exec_enabled": True}
        coordinator = Coordinator(bundle, Snapshot(args.snapshot.resolve(), manifest), inventory, **exec_options)
        with tempfile.TemporaryDirectory(prefix="bravo-agent-") as directory:
            runtime = Path(directory)
            environment = isolated_environment(dict(os.environ), runtime, args.provider)
            # Only the dedicated CLI owns its process. Library callers must launch an isolated process.
            os.environ.clear()
            os.environ.update(environment)
            if args.provider in {"openai", "chatgpt-plan"}:
                from .openai_provider import OpenAIProvider

                provider = OpenAIProvider(args.model, subscription=args.provider == "chatgpt-plan")
            else:
                from .sdk import ClaudeProvider

                provider = ClaudeProvider(runtime, args.model)
            result = asyncio.run(coordinator.propose(provider))
        # Never overwrite an earlier reviewed artifact.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print("state=" + result["state"])
        return 0 if result["state"] == "PROPOSAL_READY" else 2
    except Exception:
        # Pydantic/provider exception strings can echo unsafe input or credential values.
        print("NEEDS_HUMAN: input, policy, model, or output failure")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
