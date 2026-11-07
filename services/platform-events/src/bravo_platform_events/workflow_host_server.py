"""Single-process entry point for the workflow host (ADR-0031). No HTTP listener.

    python -m bravo_platform_events.workflow_host_server --ports deployment.module:factory

`factory(store)` is reviewed deployment code returning
`{"cutover": CutoverService, "readers": {...}, "preparers": {...}}`;
it owns the ApplicationHost (receipt PVC lock), GitHub App, T19/T36 ports. Run one replica
(Recreate): the ApplicationHost lock refuses a second process anyway.
"""

import argparse
import importlib
import logging
import re
import signal
import threading

from .workflow import WorkflowRecovery
from .workflow_host import StageReaders, WorkflowHost


def build(store, create_ports):
    ports = create_ports(store)
    if type(ports) is not dict or set(ports) - {"cutover", "readers", "preparers"} or "cutover" not in ports:
        raise ValueError("invalid_workflow_ports")
    if ports["cutover"].store is not store:
        raise ValueError("shared_plan_store_required")
    readers = StageReaders(ports.get("readers", {}), preparers=ports.get("preparers", {}))
    return WorkflowHost(WorkflowRecovery(ports["cutover"], read_evidence=readers))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", required=True, help="reviewed deployment module:factory(store)")
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.ports):
            raise ValueError()
        module, factory = args.ports.split(":")
        create_ports = getattr(importlib.import_module(module), factory)
        from .postgres import PostgresPlanStore
        from .runtime_probe import connection_checks
        from .window_server import runtime_connection

        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 3}]:
                raise ValueError()
        host = build(store, create_ports)
    except Exception as error:
        from .startup import reason

        raise SystemExit("workflow_host_startup_unavailable:" + reason(error)) from None
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    host.run(stop, interval=args.interval)


if __name__ == "__main__":
    main()
