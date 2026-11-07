"""A one-shot platform process; no ambient developer settings or credentials."""

import argparse
import asyncio
import contextlib
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


def write_state(path: Path, state: dict, replace: bool) -> None:
    """Publish one complete 0600 state file; a crash leaves the previous complete state.

    The first write links a uniquely named temporary file, which fails if anything (even a dangling
    symlink) took the name meanwhile; later writes atomically replace the file this run published.
    """
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
    analyze.add_argument("--state-output", type=Path,
                         help="New file for the write-ahead coordinator state (T10 persists it)")
    analyze.add_argument("--reasoning-effort",
                         help="Reasoning effort from the environment's model config; omitted = provider default")
    analyze.add_argument("--sandbox-image",
                         help="Agents SDK Docker sandbox image (digest-pinned) from the environment config")
    args = parser.parse_args()
    if args.command == "schema":
        print(json.dumps(Proposal.model_json_schema(), indent=2))
        return 0
    try:
        # lexists: a dangling symlink is an existing path, never a file to create through.
        if any(path is not None and os.path.lexists(path) for path in (args.output, args.state_output)):
            raise ValueError("output already exists")
        bundle = FailureBundle.model_validate(bounded_json(args.bundle, 16384))
        manifest = SnapshotManifest.model_validate(bounded_json(args.manifest, 32768))
        # Only the model-facing subset survives; a different revision is refused like any input error.
        inventory = (InventorySummary.from_document(bounded_json(args.inventory, 262144))
                     if args.inventory is not None else None)
        checkpoint = None
        if args.state_output is not None:
            state_output = args.state_output.parent.resolve() / args.state_output.name
            published = False

            def checkpoint(state):
                nonlocal published
                write_state(state_output, state, replace=published)
                published = True
        coordinator = Coordinator(bundle, Snapshot(args.snapshot.resolve(), manifest), inventory,
                                  checkpoint=checkpoint)
        with tempfile.TemporaryDirectory(prefix="bravo-agent-") as directory:
            runtime = Path(directory)
            environment = isolated_environment(dict(os.environ), runtime, args.provider)
            # Only the dedicated CLI owns its process. Library callers must launch an isolated process.
            os.environ.clear()
            os.environ.update(environment)
            if args.provider in {"openai", "chatgpt-plan"}:
                from .openai_provider import OpenAIProvider

                provider = OpenAIProvider(args.model, subscription=args.provider == "chatgpt-plan",
                                          reasoning_effort=args.reasoning_effort, sandbox_image=args.sandbox_image)
            else:
                from .sdk import ClaudeProvider

                if args.sandbox_image is not None:
                    raise ValueError("the Docker sandbox runs on the Agents SDK provider")
                provider = ClaudeProvider(runtime, args.model, args.reasoning_effort)
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
