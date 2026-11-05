"""Test-only TCP denial probe; exit 0 denied, 1 reachable, 2 inconclusive.

DNS, process startup and unexpected socket errors are never denial evidence.
A denial result is useful only with the caller's healthy endpoint controls.
No HTTP status (including 401/503) can make an open transport pass this probe.
"""

import errno
import json
import socket
import sys
import time


def probe(host: str, port: int, *, attempts: int = 8, timeout: float = 2,
          interval: float = 1) -> tuple[int, dict]:
    if attempts < 1 or timeout <= 0 or interval < 0:
        return 2, {"status": "inconclusive", "reason": "invalid_bounds", "attempts": 0}
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return 2, {"status": "inconclusive", "reason": "dns", "attempts": 0}
    if not addresses:
        return 2, {"status": "inconclusive", "reason": "no_addresses", "attempts": 0}
    failures = []
    for attempt in range(attempts):
        for family, socktype, proto, _, address in addresses:
            try:
                with socket.socket(family, socktype, proto) as client:
                    client.settimeout(timeout)
                    client.connect(address)
                return 1, {"status": "reachable", "attempts": attempt + 1}
            except TimeoutError:
                failures.append("timeout")
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    failures.append("refused")
                else:
                    return 2, {"status": "inconclusive", "reason": "socket_error",
                               "attempts": attempt + 1}
        if attempt + 1 < attempts:
            time.sleep(interval)
    return 0, {"status": "transport_denied", "attempts": attempts, "failures": failures}


if __name__ == "__main__":
    code, result = probe(sys.argv[1], int(sys.argv[2]))
    print(json.dumps(result, sort_keys=True))
    sys.exit(code)
