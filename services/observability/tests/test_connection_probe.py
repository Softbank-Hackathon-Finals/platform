"""A failed HTTP request must not pass the network denial test."""

import errno
import importlib.util
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "connection_probe", Path(__file__).parents[1] / "testing/connection_probe.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_open_http_503_is_reachable_not_denied():
    class Unavailable(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.end_headers()

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Unavailable) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            code, result = module.probe("127.0.0.1", server.server_port)
            assert code == 1
            assert result["status"] == "reachable"
        finally:
            server.shutdown()
            worker.join()


def test_dns_failure_is_inconclusive():
    with patch.object(module.socket, "getaddrinfo", side_effect=socket.gaierror()):
        code, result = module.probe("unused", 8080)
    assert code == 2
    assert result["reason"] == "dns"


def test_unexpected_socket_failure_is_inconclusive():
    with patch.object(module.socket, "socket", side_effect=PermissionError()):
        code, result = module.probe("127.0.0.1", 8080)
    assert code == 2
    assert result["reason"] == "socket_error"


def test_later_success_cannot_be_hidden_by_first_refusal():
    with patch.object(module.socket, "socket") as factory:
        client = factory.return_value.__enter__.return_value
        client.connect.side_effect = [ConnectionRefusedError(errno.ECONNREFUSED, "refused"), None]
        code, result = module.probe("127.0.0.1", 8080, interval=0)
    assert code == 1
    assert result["attempts"] == 2


def test_denial_needs_every_attempt_and_does_not_claim_policy_by_itself():
    with patch.object(module.socket, "socket") as factory:
        client = factory.return_value.__enter__.return_value
        client.connect.side_effect = TimeoutError()
        code, result = module.probe("127.0.0.1", 8080, interval=0)
    assert code == 0
    assert result["status"] == "transport_denied"
    assert len(result["failures"]) == 8
