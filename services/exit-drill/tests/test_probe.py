"""접속 시험 도구. 실제 소켓으로 잰다. 대상은 전부 이 프로세스가 연 로컬 포트다."""

import errno
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bravo_exit_drill import probe


@pytest.fixture
def listener():
    server = socket.create_server(("127.0.0.1", 0))
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def closed_port():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ws":
            if self.headers.get("Upgrade", "").lower() == "websocket" and self.headers.get("Sec-WebSocket-Key"):
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.end_headers()
                return
            self.send_error(400)
            return
        self.send_response(200 if self.path == "/ok" else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def http_port():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_open_port_is_connected(listener):
    assert probe.tcp_state("127.0.0.1", listener, timeout=1) == "connected"


def test_closed_port_is_refused(closed_port):
    assert probe.tcp_state("127.0.0.1", closed_port, timeout=1) == "refused"


def test_silent_drop_is_timeout():
    def drop(*_args, **_kwargs):
        raise TimeoutError

    assert probe.tcp_state("127.0.0.1", 1, timeout=1, connect=drop) == "timeout"


def test_no_route_is_unreachable():
    def no_route(*_args, **_kwargs):
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    assert probe.tcp_state("127.0.0.1", 1, timeout=1, connect=no_route) == "unreachable"


def test_name_failure_is_not_denial():
    def bad_name(*_args, **_kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    assert probe.tcp_state("x.invalid", 1, timeout=1, connect=bad_name) == "dns_failure"


def test_other_os_error_is_error():
    def weird(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    assert probe.tcp_state("127.0.0.1", 1, timeout=1, connect=weird) == "error"


def test_tcp_probe_sends_no_data():
    """출발지 경로에는 연결만 시도하고 아무것도 보내지 않는다."""
    server = socket.create_server(("127.0.0.1", 0))
    received = []

    def accept():
        conn, _ = server.accept()
        conn.settimeout(1)
        try:
            received.append(conn.recv(16))
        except TimeoutError:
            received.append(b"timeout")
        conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    assert probe.tcp_state("127.0.0.1", server.getsockname()[1], timeout=1) == "connected"
    thread.join(2)
    server.close()
    assert received == [b""]


def test_dns_state_resolved_and_failed():
    assert probe.dns_state("localhost") == "resolved"

    def bad(*_args, **_kwargs):
        raise socket.gaierror(-2, "no")

    assert probe.dns_state("x.invalid", resolve=bad) == "dns_failure"


def test_http_check_reports_status(http_port):
    assert probe.http_status("127.0.0.1", http_port, "/ok", timeout=1) == 200
    assert probe.http_status("127.0.0.1", http_port, "/nope", timeout=1) == 503


def test_http_check_on_closed_port_is_none(closed_port):
    assert probe.http_status("127.0.0.1", closed_port, "/ok", timeout=1) is None


def test_websocket_upgrade_reports_status(http_port):
    assert probe.websocket_status("127.0.0.1", http_port, "/ws", timeout=1) == 101
    assert probe.websocket_status("127.0.0.1", http_port, "/ok", timeout=1) == 200


def test_websocket_on_closed_port_is_none(closed_port):
    assert probe.websocket_status("127.0.0.1", closed_port, "/ws", timeout=1) is None


def test_run_keeps_order_and_times_every_target(listener, closed_port):
    results = probe.run([
        {"route_id": "a", "kind": "tcp", "host": "127.0.0.1", "port": listener},
        {"route_id": "b", "kind": "tcp", "host": "127.0.0.1", "port": closed_port},
        {"route_id": "c", "kind": "dns", "host": "localhost"},
    ], timeout=1)
    assert [(r["route_id"], r["state"]) for r in results] == [
        ("a", "connected"), ("b", "refused"), ("c", "resolved"),
    ]
    assert all(r["observed_at"].endswith("+00:00") for r in results)
    assert [r["observed_at"] for r in results] == sorted(r["observed_at"] for r in results)


def test_run_marks_check_results_ok(http_port):
    results = probe.run([
        {"check_id": "good", "kind": "http", "host": "127.0.0.1", "port": http_port, "path": "/ok"},
        {"check_id": "bad", "kind": "http", "host": "127.0.0.1", "port": http_port, "path": "/nope"},
        {"check_id": "socket", "kind": "websocket", "host": "127.0.0.1", "port": http_port, "path": "/ws"},
        {"check_id": "not-upgraded", "kind": "websocket", "host": "127.0.0.1", "port": http_port, "path": "/ok"},
    ], timeout=1)
    assert [(r["check_id"], r["ok"]) for r in results] == [
        ("good", True), ("bad", False), ("socket", True), ("not-upgraded", False),
    ]


def test_run_refuses_to_become_a_port_scanner():
    many = [{"route_id": str(i), "kind": "dns", "host": "localhost"} for i in range(probe.MAX_TARGETS + 1)]
    with pytest.raises(ValueError):
        probe.run(many, timeout=1)


def test_run_refuses_unknown_kind():
    with pytest.raises(ValueError):
        probe.run([{"route_id": "x", "kind": "shell", "host": "localhost"}], timeout=1)


def test_module_runs_alone_inside_a_pod_with_only_python(listener):
    """Pod 안에서는 이 파일 하나만 표준 입력으로 넘겨 실행한다. 패키지가 없어도 돌아야 한다."""
    source = Path(probe.__file__).read_text(encoding="utf-8")
    targets = [{"route_id": "a", "kind": "tcp", "host": "127.0.0.1", "port": listener}]
    done = subprocess.run(
        [sys.executable, "-I", "-", json.dumps({"targets": targets, "timeout": 1})],
        input=source, capture_output=True, text=True, timeout=20, check=True,
    )
    results = json.loads(done.stdout)
    assert [(r["route_id"], r["state"]) for r in results] == [("a", "connected")]
