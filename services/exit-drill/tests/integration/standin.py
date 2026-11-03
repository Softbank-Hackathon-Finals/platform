"""k3s 시험에 쓰는 대역 프로그램. ROLE 환경 변수로 역할을 정한다. 시험 장치이며 운영에 쓰지 않는다.

실제 demo-chat · PgBouncer · CNPG · Tailscale이 아니다. 잴 수 있는 것은 경로가 열리고 닫히는 것이고,
제품 자체의 동작은 재지 못한다. 그 한계는 증거 문서에 적는다.
"""

import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STREAM_INTERVAL_S = 0.2
STREAM_STALE_S = 2.0
CONNECT_TIMEOUT_S = 3


def lsn_now() -> str:
    """재시작해도 뒤로 가지 않는 LSN 대역값."""
    value = int(time.time() * 1000)
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


class _Threaded(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_tcp(port: int, handler) -> None:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                handler(self.request)
            except OSError:
                pass

    threading.Thread(target=_Threaded(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()


def serve_http(port: int, routes: dict) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = routes.get(self.path, lambda: (404, {}))()
            raw = json.dumps(body).encode()
            self.send_response(status)
            if status == 101:
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
            else:
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if status != 101:
                self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()


def echo(connection: socket.socket) -> None:
    while data := connection.recv(1024):
        connection.sendall(data)


def relay_to(host: str, port: int):
    def relay(connection: socket.socket) -> None:
        upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        upstream.settimeout(None)

        def pump(source, sink):
            try:
                while data := source.recv(4096):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                sink.close()

        threading.Thread(target=pump, args=(upstream, connection), daemon=True).start()
        pump(connection, upstream)

    return relay


def run_tcp() -> None:
    for port in os.environ["PORTS"].split(","):
        serve_tcp(int(port), echo)


def run_relay() -> None:
    serve_tcp(int(os.environ["LISTEN_PORT"]), relay_to(os.environ["UPSTREAM_HOST"], int(os.environ["UPSTREAM_PORT"])))


def run_db() -> None:
    """앱 경로에는 PONG을, 복제 경로에는 LSN을 준다. /freeze로 쓰기 없는 상태를 만든다."""
    frozen = {"lsn": None}

    def handle(connection: socket.socket) -> None:
        first = connection.makefile().readline().strip()
        if first == "PING":
            connection.sendall(b"PONG target-db\n")
        elif first == "REPLICATE":
            while True:
                connection.sendall(f"LSN {frozen['lsn'] or lsn_now()}\n".encode())
                time.sleep(STREAM_INTERVAL_S)

    def freeze():
        frozen["lsn"] = lsn_now()
        return 200, {"frozen": frozen["lsn"]}

    def thaw():
        frozen["lsn"] = None
        return 200, {"frozen": None}

    serve_tcp(5432, handle)
    serve_http(9187, {"/freeze": freeze, "/thaw": thaw})


def run_source() -> None:
    """출발지 대역. 정방향 경로의 연결을 받고, 역방향으로 목적지에 접속해 LSN을 받는다."""
    state = {"lsn": None, "last": 0.0}

    def subscribe() -> None:
        host, port = os.environ["REVERSE_HOST"], int(os.environ["REVERSE_PORT"])
        while True:
            try:
                with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S) as connection:
                    connection.sendall(b"REPLICATE\n")
                    connection.settimeout(STREAM_STALE_S)
                    reader = connection.makefile()
                    while line := reader.readline():
                        state.update(lsn=line.split()[1], last=time.monotonic())
            except OSError:
                pass
            time.sleep(0.5)

    def report():
        streaming = time.monotonic() - state["last"] < STREAM_STALE_S
        return 200, {"state": "streaming" if streaming else "stopped", "lsn": state["lsn"]}

    serve_tcp(5432, echo)
    serve_http(9187, {"/replication": report})
    threading.Thread(target=subscribe, daemon=True).start()


def run_app() -> None:
    """demo-chat의 경로 이름을 따른다. /readyz가 DB를 보지 않는 것도 실제 앱과 같다."""
    pooler = (os.environ["POOLER_HOST"], int(os.environ["POOLER_PORT"]))
    required = os.environ.get("REQUIRE_AT_START")
    if required:
        # 고정 소스 ce5727e처럼 시작할 때 AWS에 닿아야 뜨는 앱을 흉내 낸다.
        host, _, port = required.rpartition(":")
        try:
            socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT_S).close()
        except OSError:
            sys.exit(1)

    held = {"alive": False}
    target = os.environ.get("HOLD_HOST")

    def hold() -> None:
        """시작할 때 연 연결을 계속 쓴다. 정책을 넣은 뒤 기존 연결이 끊기는지 재기 위한 것이다."""
        try:
            with socket.create_connection((target, 443), timeout=CONNECT_TIMEOUT_S) as connection:
                connection.settimeout(CONNECT_TIMEOUT_S)
                while True:
                    connection.sendall(b"x")
                    held["alive"] = connection.recv(1) == b"x"
                    if not held["alive"]:
                        return
                    time.sleep(1)
        except OSError:
            held["alive"] = False

    if target:
        threading.Thread(target=hold, daemon=True).start()

    def database_reachable() -> bool:
        try:
            with socket.create_connection(pooler, timeout=CONNECT_TIMEOUT_S) as connection:
                connection.settimeout(CONNECT_TIMEOUT_S)
                connection.sendall(b"PING\n")
                return connection.recv(64).startswith(b"PONG")
        except OSError:
            return False

    serve_http(8080, {
        "/healthz": lambda: (200, {"ok": True}),
        "/readyz": lambda: (200, {"ok": True}),
        "/api/health/db": lambda: (200, {"ok": True}) if database_reachable() else (503, {"ok": False}),
        "/ws": lambda: (101, {}) if database_reachable() else (503, {"ok": False}),
        "/debug/held": lambda: (200, dict(held)),
    })


def main() -> None:
    # 컨테이너의 1번 프로세스는 처리기가 없으면 신호를 무시한다. 컨테이너만 다시 띄우는 시험에 필요하다.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    {"tcp": run_tcp, "relay": run_relay, "db": run_db, "source": run_source, "app": run_app}[os.environ["ROLE"]]()
    threading.Event().wait()


if __name__ == "__main__":
    main()
