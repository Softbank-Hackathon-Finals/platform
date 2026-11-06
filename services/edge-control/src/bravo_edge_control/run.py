"""Reviewed host entry point for one registered LB and one persistent journal."""

import argparse
import importlib
import json
import re
import signal
from pathlib import Path
from threading import Event

from .authority import ShiftAuthority
from .control import _now
from .host import ShiftHost
from .provider import Binding, Cloudflare
from .runtime import RuntimePorts, ShiftRuntime


def load_runtime(store, config_file, create_ports, *, clock=_now):
    config = json.loads(Path(config_file).read_text())
    required = {"version", "plan_id", "run_id", "profile", "lb_binding", "journal"}
    if (type(config) is not dict or set(config) - required - {"token_file"}
            or required - set(config) or type(config["version"]) is not int
            or config["version"] != 1):
        raise ValueError("invalid_shift_runtime_configuration")
    journal = Path(config["journal"])
    if not journal.is_absolute() or not journal.parent.is_dir():
        raise ValueError("persistent_journal_mount_required")
    binding = Binding(**config["lb_binding"])
    ports = create_ports(store)
    if not isinstance(ports, RuntimePorts):
        raise ValueError("current_runtime_ports_required")
    provider = ports.provider
    if provider is None:
        token_file = Path(config["token_file"])
        if not token_file.is_absolute() or not token_file.is_file():
            raise ValueError("provider_credential_mount_required")
        provider = Cloudflare(binding, token=lambda: token_file.read_text())
    if provider.binding != binding:
        raise ValueError("registered_provider_binding_required")
    authority = ShiftAuthority(store, run_id=config["run_id"], load_session=ports.load_session, clock=clock)
    host = ShiftHost(store, provider, journal, authority=authority, clock=clock)
    return ShiftRuntime(host, principal=ports.principal, plan_id=config["plan_id"],
                        run_id=config["run_id"]), authority, config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ports", required=True, help="reviewed module:factory(store)")
    parser.add_argument("action", choices=("register", "sequence", "watch"))
    args = parser.parse_args(argv)
    runtime = None
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.ports):
            raise ValueError("invalid_runtime_ports")
        from bravo_platform_events.postgres import PostgresPlanStore
        from bravo_platform_events.runtime_probe import connection_checks
        from bravo_platform_events.window_server import runtime_connection

        module, name = args.ports.split(":")
        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        runtime, authority, config = load_runtime(store, args.config,
            getattr(importlib.import_module(module), name))
        def emit(value):
            print(json.dumps(value, separators=(",", ":")), flush=True)
        if args.action == "register":
            authority.register(runtime.principal(), runtime.plan_id, run_id=config["run_id"],
                profile=config["profile"], binding=runtime.host.provider.binding)
            emit(dict(status="REGISTERED"))
        else:
            stop = Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            result = getattr(runtime, args.action)(stop=stop, emit=emit)
            emit(dict(result=result))
            if (result is None or result.get("status") == "STOPPED"
                    or (result.get("rollback") and result["rollback"]["status"] != "CONFIRMED")):
                raise SystemExit(2)
    except Exception:
        # Do not print token, DB DSN, provider response, or arbitrary factory text.
        raise SystemExit("shift_runtime_unavailable") from None
    finally:
        if runtime is not None:
            runtime.host.provider.close()


if __name__ == "__main__":
    main()
