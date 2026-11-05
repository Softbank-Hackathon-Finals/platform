"""One-observation entry point for independent credential verification Jobs.

The job launcher owns Kubernetes identity, image digest and UID enforcement.
This command returns no secret, token, raw server response or approval decision.
"""

import argparse
import json
import sys

from .app_health_probe import observe_app_database
from .kube_secret_probe import observe_secret_get


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe a scoped credential rehearsal check")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    secret = subparsers.add_parser("secret-get")
    secret.add_argument("--namespace", required=True)
    secret.add_argument("--name", required=True)
    app = subparsers.add_parser("app-db")
    app.add_argument("--namespace", required=True)
    app.add_argument("--service", required=True)
    app.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "secret-get":
            result = observe_secret_get(args.namespace, args.name)
        else:
            result = observe_app_database(args.namespace, args.service, args.port)
    except Exception:
        result = "unknown"
    print(json.dumps({"observation": result}, separators=(",", ":")))
    return 2 if result == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
