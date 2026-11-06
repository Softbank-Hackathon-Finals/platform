"""Read-only plan state for operators (`python -m bravo_platform_events.plan_state <plan_id>`).

Runs where the T10 database credentials already are (Reader or workflow host Pod). Prints one
line {"plan_id", "state", "version"}; never writes. Exit 1 when the plan is not found.
"""

import json
import sys


def read(store, plan_id):
    snapshot = store.snapshot(plan_id)
    return {"plan_id": plan_id, "state": snapshot["state"], "version": snapshot["version"]}


def main(argv=None):
    from .postgres import PostgresPlanStore
    from .window_server import runtime_connection

    argv = sys.argv[1:] if argv is None else argv
    try:
        print(json.dumps(read(PostgresPlanStore(runtime_connection()), argv[0])))
    except Exception as error:  # noqa: BLE001 - one bounded code for the caller
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
