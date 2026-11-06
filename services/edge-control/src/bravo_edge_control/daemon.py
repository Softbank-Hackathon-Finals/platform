"""One persistent Kubernetes worker for a deployment-selected SHIFT run.

The existing host authenticates every action. This worker never grants authority,
registers a run, changes plan state or retries an uncertain provider write.
"""

import argparse
import importlib
import json
import os
import re
import signal
import time
from pathlib import Path
from threading import Event

from .provider import Cloudflare
from .run import load_runtime


def status(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"state": state, "observed_at": time.time()}))
    os.replace(temporary, path)


def healthy(path, *, live=False, clock=time.time):
    try:
        value = json.loads(path.read_text())
        if not 0 <= clock() - value["observed_at"] <= 90:
            return False
        return live or value["state"] in {"RUNNING", "RESTORED", "ABORTED"}
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serve(runtime, *, stop, health_file, interval=2):
    """Wait for the existing registration; retain terminal state until redeployed."""
    terminal = None
    while not stop.is_set():
        if terminal is not None:
            status(health_file, terminal)
            stop.wait(interval)
            continue
        status(health_file, "WAITING")
        try:
            result = runtime.sequence(stop=stop, emit=lambda _: status(health_file, "RUNNING"))
            value = result.get("status") if isinstance(result, dict) else None
            terminal = value if value in {"RESTORED", "ABORTED"} else "STOPPED"
        except Exception:
            # A failed call after any admitted operation is not fresh permission.
            # The durable journal, not a restart, decides if an effect is uncertain.
            try:
                previous = runtime.host._controller(lambda _: False).current()
            except Exception:
                previous = True
            if previous:
                terminal = "STOPPED"
        status(health_file, terminal or "WAITING")
        stop.wait(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-file", type=Path, default=Path("/tmp/edge-health.json"))
    parser.add_argument("--config")
    parser.add_argument("--ports")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("action", choices=("serve", "health"))
    args = parser.parse_args(argv)
    if args.action == "health":
        raise SystemExit(0 if healthy(args.health_file, live=args.live) else 1)
    runtime = None
    try:
        if not args.config or not args.ports or not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.ports):
            raise ValueError("deployment_ports_required")
        from bravo_platform_events.postgres import PostgresPlanStore
        from bravo_platform_events.runtime_probe import connection_checks
        from bravo_platform_events.window_server import runtime_connection

        module, factory = args.ports.split(":")
        connection = runtime_connection()
        connection_checks(connection)
        settings = json.loads(Path(args.config).read_text())
        if (settings.get("journal") != "/journal/edge.db"
                or settings.get("token_file") != "/provider/token"):
            raise ValueError("controller_persistent_mounts_required")
        runtime, _, _ = load_runtime(PostgresPlanStore(connection), args.config,
                                    getattr(importlib.import_module(module), factory))
        if not isinstance(runtime.host.provider, Cloudflare):
            raise ValueError("native_provider_required")
        runtime.host.provider.read()
        stop = Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        serve(runtime, stop=stop, health_file=args.health_file)
    except Exception:
        status(args.health_file, "UNAVAILABLE")
        raise SystemExit("edge_controller_unavailable") from None
    finally:
        if runtime is not None:
            runtime.host.provider.close()


if __name__ == "__main__":
    main()
