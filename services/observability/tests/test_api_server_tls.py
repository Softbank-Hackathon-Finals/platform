"""Real local TLS handshakes; synthetic store, no production credentials."""

import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from bravo_observability.api import bearer_token, create_app, server_config
from bravo_observability.healthcheck import check

TOKEN = "local-qualification-" + "x" * 32


class EmptyStore:
    def query(self, _query):
        return []


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    root = tmp_path_factory.mktemp("api-server-tls")
    script = Path(__file__).parents[1] / "deploy/certs/issue.sh"
    for args in (
        ("ca", str(root / "ca"), "3"),
        ("ca", str(root / "wrong-ca"), "3"),
        (
            "leaf",
            str(root / "ca"),
            "api",
            "DNS:api,DNS:localhost,IP:127.0.0.1",
            str(root / "server"),
            "1",
        ),
    ):
        subprocess.run(["bash", str(script), *args], check=True, capture_output=True, timeout=10)
    return root


@pytest.fixture
def live_server(certificates, monkeypatch):
    monkeypatch.setenv("BRAVO_API_SERVER_TLS_DIR", str(certificates / "server"))
    monkeypatch.setenv("BRAVO_API_SERVER_NAME", "localhost")
    app = create_app(EmptyStore(), ["aws", "onprem"], authorize=bearer_token(TOKEN))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        monkeypatch.setenv("PORT", str(port))
        server = uvicorn.Server(server_config(app))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield port
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()


def test_tls_auth_and_missing_metrics(live_server, certificates):
    context = ssl.create_default_context(cafile=str(certificates / "ca/ca.crt"))
    with httpx.Client(
        base_url=f"https://127.0.0.1:{live_server}", verify=context, trust_env=False
    ) as client:
        assert client.get("/healthz").status_code == 200
        route = "/v1/observability/environments"
        assert client.get(route).status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer invalid"}).status_code == 401
        reply = client.get(route, headers={"Authorization": "Bearer " + TOKEN})
        assert reply.status_code == 200
        assert all(row["state"] == "UNOBSERVABLE" for row in reply.json()["environments"])
    check()


@pytest.mark.parametrize("kind", ["wrong-ca", "wrong-name", "tls12"])
def test_bad_tls_rejected_before_credentials(live_server, certificates, kind):
    ca = certificates / ("wrong-ca" if kind == "wrong-ca" else "ca") / "ca.crt"
    ctx = ssl.create_default_context(cafile=str(ca))
    if kind == "tls12":
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", live_server), timeout=3) as raw:
        with pytest.raises(ssl.SSLError):
            ctx.wrap_socket(
                raw, server_hostname="untrusted.invalid" if kind == "wrong-name" else "localhost"
            )
    # No HTTP bytes or Authorization header were sent before the failed handshake.


def test_plain_http_does_not_reach_authorizer(live_server):
    with httpx.Client(trust_env=False, timeout=3) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            client.get(f"http://127.0.0.1:{live_server}/healthz")


def test_healthcheck_does_not_ignore_server_name(live_server, monkeypatch):
    monkeypatch.setenv("BRAVO_API_SERVER_NAME", "untrusted.invalid")
    with pytest.raises(ssl.SSLCertVerificationError):
        check()


@pytest.mark.parametrize("directory", ["", "relative", "/does-not-exist"])
def test_incomplete_tls_cannot_fall_back_to_plaintext(directory, monkeypatch):
    monkeypatch.setenv("BRAVO_API_SERVER_TLS_DIR", directory)
    with pytest.raises((ValueError, FileNotFoundError)):
        server_config(create_app(EmptyStore(), ["aws"]))


def test_mismatched_key_stops_before_bind(certificates, tmp_path, monkeypatch):
    (tmp_path / "tls.crt").write_bytes((certificates / "server/tls.crt").read_bytes())
    (tmp_path / "tls.key").write_bytes((certificates / "wrong-ca/ca.key").read_bytes())
    monkeypatch.setenv("BRAVO_API_SERVER_TLS_DIR", str(tmp_path))
    with pytest.raises(ssl.SSLError):
        server_config(create_app(EmptyStore(), ["aws"]))


def test_legacy_local_test_mode_remains_explicitly_without_tls(monkeypatch):
    monkeypatch.delenv("BRAVO_API_SERVER_TLS_DIR", raising=False)
    config = server_config(create_app(EmptyStore(), ["aws"]))
    assert config.ssl is None
    assert config.proxy_headers is False
