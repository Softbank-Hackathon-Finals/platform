"""접속 시험. 이 모듈만 네트워크에 닿는다.

Pod 안에서 쓰는 도구라 표준 라이브러리만 쓰고 이 파일 하나로 돌아간다:
    python3 - '{"targets": [...], "timeout": 3}' < probe.py

정해진 대상에 연결만 시도한다. tcp 시험은 아무것도 보내지 않고 바로 닫으며, 계정 · 비밀을 받지
않는다. 상태 판정은 하지 않는다. 그 일은 verdict.py가 한다.
"""

import errno
import http.client
import json
import socket
import sys
from datetime import datetime, timezone

MAX_TARGETS = 32
_UNREACHABLE = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM}
_WEBSOCKET_KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455의 예시 값. 비밀이 아니다.


def tcp_state(host, port, *, timeout, connect=socket.create_connection):
    try:
        connect((host, port), timeout=timeout).close()
    except socket.gaierror:
        return "dns_failure"
    except (TimeoutError, socket.timeout):  # noqa: UP041 - Pod의 Python 3.9 이하에서는 서로 다른 예외다
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as exc:
        return "unreachable" if exc.errno in _UNREACHABLE else "error"
    return "connected"


def dns_state(name, *, resolve=socket.getaddrinfo):
    try:
        resolve(name, None)
    except OSError:
        return "dns_failure"
    return "resolved"


def http_status(host, port, path, *, timeout):
    return _request(host, port, path, {"Connection": "close"}, timeout)


def websocket_status(host, port, path, *, timeout):
    """업그레이드 응답(101)까지만 본다. 새 연결이 닿는지 확인하는 것이 목적이다."""
    headers = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": _WEBSOCKET_KEY,
    }
    return _request(host, port, path, headers, timeout)


def _request(host, port, path, headers, timeout):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers=headers)
        return connection.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def run(targets, *, timeout):
    """대상을 순서대로 한 번씩 잰다. 포트 스캔으로 쓰이지 않게 개수를 묶는다."""
    if len(targets) > MAX_TARGETS:
        raise ValueError("too many probe targets")
    return [_measure(target, timeout) for target in targets]


def _measure(target, timeout):
    kind = target.get("kind")
    result = {key: target[key] for key in ("route_id", "check_id", "kind") if key in target}
    if kind == "tcp":
        result["state"] = tcp_state(target["host"], target["port"], timeout=timeout)
    elif kind == "dns":
        result["state"] = dns_state(target["host"])
    elif kind == "http":
        result["status"] = http_status(target["host"], target["port"], target["path"], timeout=timeout)
        result["ok"] = result["status"] == 200
    elif kind == "websocket":
        result["status"] = websocket_status(target["host"], target["port"], target["path"], timeout=timeout)
        result["ok"] = result["status"] == 101
    else:
        raise ValueError("unknown probe kind")
    result["observed_at"] = datetime.now(timezone.utc).isoformat()  # noqa: UP017 - Pod의 Python 3.10 이하에도 돌아야 한다
    return result


def main(argv):
    request = json.loads(argv[1])
    print(json.dumps(run(request["targets"], timeout=request.get("timeout", 3))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
