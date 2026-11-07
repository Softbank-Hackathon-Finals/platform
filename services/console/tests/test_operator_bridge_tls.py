"""Real loopback TLS handshakes with generated fixture certificates, no cloud credentials."""

import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from bravo_console.operator_bridge import OperatorBridge

ORIGIN = "http://127.0.0.1:18123"
TLS_NAME = "operator-upstream.test.invalid"


@pytest.fixture
def tls_upstream(tmp_path):
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(TLS_NAME)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            body = b'{"connected":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", cert_path, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        key_path.unlink()


@pytest.mark.parametrize("boundary", ["verified", "unknown-ca", "wrong-name"])
def test_cloud_proof_is_not_delivered_before_real_tls_verification(tls_upstream, boundary):
    upstream, cert_path, received = tls_upstream
    tls = (
        ssl.create_default_context(cafile=str(cert_path))
        if boundary != "unknown-ca"
        else ssl.create_default_context()
    )
    app = OperatorBridge(
        origin=ORIGIN,
        upstream=upstream,
        tls_context=tls,
        tls_server_name=TLS_NAME if boundary != "wrong-name" else "other.test.invalid",
        proof=lambda: "fixture-proof-not-a-real-credential",
    )
    with TestClient(app, base_url=ORIGIN, headers={"Sec-Fetch-Site": "same-origin"}) as client:
        response = client.get(
            "/_operator/" + app.bootstrap,
            follow_redirects=False,
            headers={"Sec-Fetch-Site": "none"},
        )
        assert response.status_code == 303
        response = client.get("/console/")
    if boundary == "verified":
        assert response.status_code == 200
        assert received == ["Bearer fixture-proof-not-a-real-credential"]
    else:
        assert response.status_code == 503
        assert not received
        assert "fixture-proof" not in response.text and TLS_NAME not in response.text
