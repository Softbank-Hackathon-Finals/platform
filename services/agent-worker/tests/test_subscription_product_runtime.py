import importlib.util
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import certifi
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_subscription_product import setup as setup

from bravo_agent import subscription_product_reader as reader
from bravo_agent import subscription_product_server as host

PRINCIPAL, RUNTIME, INSTANCE = "b" * 64, "c" * 40, "i-" + "a" * 17


def config(origin="https://inputs.invalid"):
    return reader.ProductReaderConfig(origin, certifi.where())


def test_token_factory_after_auth_without_persistence(setup):
    service, inputs, body, counts = setup
    tokens = []
    service.request_inputs = lambda token: tokens.append(token) or inputs
    assert service.run(body)[1]["state"] == "PROPOSAL_READY"
    assert service.run({**body, "web_identity_token": "wrong"})[0] == 403
    assert service.run(body)[1]["state"] == "PROPOSAL_READY"
    assert tokens == [body["web_identity_token"]] * 2
    assert counts == {"auth": 3, "resolve": 2, "invoke": 1}
    assert body["web_identity_token"].encode() not in service.journal.path.read_bytes()


def test_reader_factory_failure_does_not_claim(setup):
    service, _, body, counts = setup
    def unavailable(_token):
        raise RuntimeError("private detail")
    service.request_inputs = unavailable
    status, result = service.run(body)
    assert status == 409 and "private" not in json.dumps(result)
    assert counts == {"auth": 1, "resolve": 0, "invoke": 0}
    with service.journal.connect() as db:
        assert db.execute("select count(*) from proposals").fetchone()[0] == 0


@pytest.mark.parametrize("origin", ["http://inputs.invalid", "https://user@inputs.invalid", "https://x/read",
                                  "https://x/?token=x", "https://x/#x", "https://x:bad", "https://x\n"])
def test_reader_rejects_non_origin_config(origin):
    with pytest.raises(ValueError):
        config(origin)


def test_reader_fixed_path_auth_and_response():
    calls = []
    def transport(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-test-token"
        assert json.loads(request.content) == {"principal": PRINCIPAL, "input_ref": "input-1"}
        return httpx.Response(200, json={"source": "synthetic"})
    port = reader.ProductInputHTTPReader(config(), "private-test-token", httpx.MockTransport(transport))
    assert port(PRINCIPAL, "input-1") == {"source": "synthetic"}
    assert str(calls[0].url) == "https://inputs.invalid/internal/v1/product-inputs/read"
    assert "private-test-token" not in repr(port)


@pytest.mark.parametrize("status", [301, 302, 307, 403, 409, 503])
def test_reader_does_not_redirect_retry_or_disclose(status):
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(status, text="private source", headers={"location": "https://elsewhere.invalid"})
    port = reader.ProductInputHTTPReader(config(), "test-token", httpx.MockTransport(transport))
    with pytest.raises(ValueError, match="^authenticated product read unavailable$"):
        port(PRINCIPAL, "input-1")
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"x" * (reader.MAX_READ_BYTES + 1)])
def test_bad_or_large_reader_response(body):
    port = reader.ProductInputHTTPReader(config(), "test-token", httpx.MockTransport(
        lambda _: httpx.Response(200, content=body)))
    with pytest.raises(ValueError):
        port(PRINCIPAL, "input-1")


def test_invalid_lookup_never_sends_token():
    port = reader.ProductInputHTTPReader(config(), "test-token", httpx.MockTransport(
        lambda _: pytest.fail("invalid request cannot leave process")))
    with pytest.raises(ValueError):
        port("bad", "input-1")


def test_actual_tls_accepts_only_configured_ca(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    ca, private = tmp_path / "ca.pem", tmp_path / "key.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            seen.append(self.headers["Authorization"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"source":"synthetic"}')
    server = HTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(ca, private)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"https://localhost:{server.server_port}"
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    try:
        port = reader.ProductInputHTTPReader(reader.ProductReaderConfig(origin, str(ca)), "test-token")
        assert port(PRINCIPAL, "input-1") == {"source": "synthetic"}
        with pytest.raises(ValueError):
            reader.ProductInputHTTPReader(config(origin), "second-token")(PRINCIPAL, "input-1")
        assert seen == ["Bearer test-token"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_product_host_journal_and_fixture_disabled(monkeypatch, tmp_path):
    settings, seen = {"state_dir": str(tmp_path / "state")}, []
    monkeypatch.setattr(host, "server_context", lambda: "tls-context")
    def service(received, inputs, journal, *, request_inputs):
        assert received == settings and inputs is None
        seen.append(journal)
        assert request_inputs("request-only-token").read.token == "request-only-token"
        return "product"
    monkeypatch.setattr(host, "product_service", service)
    monkeypatch.setattr(host, "serve", lambda settings, **kwargs: seen.append(kwargs))
    a, b = tmp_path / "settings.json", tmp_path / "reader.json"
    a.write_text(json.dumps(settings))
    b.write_text(json.dumps({"origin": "https://inputs.invalid", "ca_file": certifi.where()}))
    host.main(a, b)
    assert seen == [tmp_path / "product-journal", {"product": "product", "fixture_enabled": False, "tls_context": "tls-context"}]










class SSM:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
    def send_command(self, **kwargs):
        operation = kwargs["Parameters"]["operation"][0]
        assert kwargs["DocumentName"] == "bravo-subscription-service"
        self.calls.append(operation)
        if self.fail == operation:
            raise RuntimeError("private cloud detail")
        return {"Command": {"CommandId": operation}}
    def get_waiter(self, _name):
        return SimpleNamespace(wait=lambda **_: None)
    def start_session(self, **kwargs):
        assert kwargs["DocumentName"] == "bravo-subscription-tunnel"
        assert kwargs["Parameters"] == {"localPortNumber": ["18762"]}
        self.calls.append("session")
        return {"SessionId": "owned-session"}
    def terminate_session(self, **kwargs):
        assert kwargs == {"SessionId": "owned-session"}
        self.calls.append("terminate")
        if self.fail == "terminate":
            raise RuntimeError("private cloud detail")










def test_install_product_opt_in_is_metadata_only():
    path = Path(__file__).resolve().parents[3] / "operations/subscription/install.py"
    spec = importlib.util.spec_from_file_location("product_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = {k: "synthetic" for k in ("secret_arn", "runtime_host_id", "broker_role_arn", "model")}
    settings["region"] = "ap-northeast-2"
    assert "subscription_product_server" not in module.installation(RUNTIME, "d" * 64, settings)
    product = module.installation(RUNTIME, "d" * 64, settings, product_reader={
        "origin": "https://inputs.invalid", "ca_file": "/etc/bravo-subscription/reader-ca.pem"})
    assert "subscription_product_server" in product and "product-journal" not in product
    with pytest.raises(TypeError):
        module.installation(RUNTIME, "d" * 64, settings, product_reader={
            "origin": "https://inputs.invalid", "ca_file": "/ca.pem", "token": "private"})







def test_missing_reader_ca_prevents_startup(monkeypatch, tmp_path):
    settings, config_file = tmp_path / "settings.json", tmp_path / "reader.json"
    settings.write_text("{}")
    config_file.write_text(json.dumps({"origin": "https://inputs.invalid", "ca_file": str(tmp_path / "missing")}))
    monkeypatch.setattr(host, "build_service", lambda *_: pytest.fail("no journal or broker before CA check"))
    with pytest.raises(FileNotFoundError):
        host.main(settings, config_file)



