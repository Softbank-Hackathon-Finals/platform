"""Run the T15 isolation gate against a previously created namespace."""

import argparse
import json
import signal
from pathlib import Path

from .isolation.__main__ import _stop
from .isolation.kube import Kubectl
from .isolation.profile import read_env, targets
from .isolation_gate import IsolationBlocked, check_isolation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--context")
    parser.add_argument("--image", default="busybox:1.36")
    args = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        result = check_isolation(Kubectl(args.context), args.namespace, targets(read_env(args.profile)), args.image)
    except (IsolationBlocked, OSError, ValueError, KeyError):
        print(json.dumps({"status": "BLOCK"}))
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({"status": "PASS", "namespace": result.namespace,
                      "namespace_uid": result.namespace_uid, "probe_run_id": result.probe_run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
