from urllib.error import HTTPError, URLError

import pytest

from rehearsal_runner.kube_secret_probe import observe_secret_get


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, *_):
        raise AssertionError("the secret body must not be read")


class Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def setup_probe(tmp_path, monkeypatch, result):
    token = tmp_path / "token"
    token.write_text("fixture-token")
    opener = Opener(result)
    monkeypatch.setattr("ssl.create_default_context", lambda **_: object())
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    return token, opener


def test_200_reports_access_without_reading_secret_body(tmp_path, monkeypatch):
    token, opener = setup_probe(tmp_path, monkeypatch, Response())
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == "yes"
    request, timeout = opener.requests[0]
    assert request.full_url == "https://kubernetes.default.svc/api/v1/namespaces/rehearsal-x/secrets/app-credentials"
    assert request.get_header("Authorization") == "Bearer fixture-token"
    assert timeout == 5


@pytest.mark.parametrize("status,expected", [(403, "no"), (401, "unknown"), (404, "unknown"),
                                               (302, "unknown"), (500, "unknown")])
def test_only_forbidden_is_denial(tmp_path, monkeypatch, status, expected):
    error = HTTPError("https://kubernetes.default.svc", status, "error", {}, None)
    token, _ = setup_probe(tmp_path, monkeypatch, error)
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == expected


def test_transport_failure_is_unknown(tmp_path, monkeypatch):
    token, _ = setup_probe(tmp_path, monkeypatch, URLError("not available"))
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == "unknown"


@pytest.mark.parametrize("namespace,secret", [("../default", "valid"), ("default", "x/y"),
                                                 ("default", "invalid..name"), ("BAD", "valid")])
def test_invalid_scopes_never_reach_api(tmp_path, monkeypatch, namespace, secret):
    token, opener = setup_probe(tmp_path, monkeypatch, Response())
    with pytest.raises(ValueError):
        observe_secret_get(namespace, secret, token_path=token, ca_path=token)
    assert opener.requests == []


def test_missing_token_is_unknown(tmp_path):
    assert observe_secret_get("default", "app", token_path=tmp_path / "missing", ca_path=tmp_path / "missing") == "unknown"


@pytest.mark.parametrize("http_status,expected", [(200, "yes"), (403, "no"), (302, "unknown")])
def test_scoped_request_uses_real_tls_and_service_account_token(tmp_path, monkeypatch, http_status, expected, capsys):
    import shutil
    import socket
    import ssl
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is required to generate the local TLS fixture")
    cert, key, token = tmp_path / "cert.pem", tmp_path / "key.pem", tmp_path / "token"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=kubernetes.default.svc", "-addext", "subjectAltName=DNS:kubernetes.default.svc",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    token.write_text("fixture-token")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            if http_status == 302:
                self.send_header("Location", "https://attacker.invalid/collect")
            self.end_headers()
            self.wfile.write(b'{"data":{"test":"private-response-value"}}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    original = socket.create_connection
    destinations = []

    def local_connection(address, *args, **kwargs):
        destinations.append(address)
        if address == ("kubernetes.default.svc", 443):
            address = ("127.0.0.1", server.server_port)
        return original(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", local_connection)
    thread.start()
    try:
        assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=cert) == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert seen == [("/api/v1/namespaces/rehearsal-x/secrets/app-credentials", "Bearer fixture-token")]
    assert destinations == [("kubernetes.default.svc", 443)]
    output = capsys.readouterr()
    assert "private-response-value" not in output.out + output.err
    assert "fixture-token" not in output.out + output.err
