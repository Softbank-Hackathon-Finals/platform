"""Real loopback TCP response loss and verified TLS configuration boundaries."""

import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_cutover import COMMAND
from test_executor import EXPECTED, response_value

from bravo_platform_events.executor import ExecutorClient


@pytest.mark.parametrize("weak", ["hostname", "certificate"])
def test_custom_ca_requires_hostname_and_certificate_verification(weak):
    context = ssl.create_default_context()
    context.check_hostname = False
    if weak == "certificate":
        context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verified_tls_context_required"):
        ExecutorClient("https://executor.example.invalid", tls_context=context)


def test_plain_cluster_http_is_not_silently_enabled():
    with pytest.raises(ValueError, match="invalid_executor_origin"):
        ExecutorClient("http://cutover-executor.bravo-platform.svc:8080")


@pytest.mark.parametrize("lose_response", [False, True])
def test_real_post_and_response_loss_query(lose_response):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            received.append((self.command, self.headers["Authorization"],
                             self.headers["Idempotency-Key"],
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if lose_response:
                self.close_connection = True
                return
            self.send_value(202)

        def do_GET(self):
            received.append((self.command,))
            self.send_value(200)

        def send_value(self, status):
            body = json.dumps(response_value()).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExecutorClient(f"http://127.0.0.1:{server.server_port}", token="fixture-bearer") as client:
            result = client.submit(EXPECTED, COMMAND, idempotency_key="command-1")
            assert result.outcome == ("UNKNOWN" if lose_response else "OBSERVED")
            assert client.read(EXPECTED).outcome == "OBSERVED"
        assert received[0] == ("POST", "Bearer fixture-bearer", "command-1", COMMAND)
        assert [r[0] for r in received] == ["POST", "GET"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_tls_requires_trusted_ca_and_matching_hostname(tmp_path):
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True, timeout=15)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps(response_value()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        trusted = ssl.create_default_context(cafile=str(certificate))
        with ExecutorClient(f"https://localhost:{server.server_port}", tls_context=trusted) as client:
            assert client.read(EXPECTED).outcome == "OBSERVED"
        with ExecutorClient(f"https://127.0.0.1:{server.server_port}", tls_context=trusted) as client:
            assert client.read(EXPECTED).reason == "transport_failure"
        with ExecutorClient(f"https://localhost:{server.server_port}") as client:
            assert client.read(EXPECTED).reason == "transport_failure"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
