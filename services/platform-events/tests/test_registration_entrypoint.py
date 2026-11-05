"""TLS stays on the canonical registration host, with no extra authentication app."""
from contextlib import contextmanager

import pytest

from bravo_platform_events import registration_server


@pytest.fixture
def host(monkeypatch):
    calls = []

    class Store:
        @contextmanager
        def _transaction(self):
            yield self

        def execute(self, _sql):
            return self

        def fetchall(self):
            return [{"version": 3}]

    monkeypatch.setattr(registration_server, "runtime_connection", lambda: "private-runtime-mount")
    monkeypatch.setattr(registration_server, "connection_checks", lambda _: None)
    monkeypatch.setattr(registration_server, "PostgresPlanStore", lambda _: Store())
    app = object()
    monkeypatch.setattr(registration_server, "build_app", lambda *_: app)
    monkeypatch.setattr(registration_server.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    return app, calls


def test_tls_host_preserves_canonical_app_and_private_logs(host):
    app, calls = host
    registration_server.main(["--tls-cert", "/tls/tls.crt", "--tls-key", "/tls/tls.key"])
    actual, options = calls[0]
    assert actual is app
    assert options["port"] == 8443 and options["workers"] == 1
    assert options["ssl_certfile"] == "/tls/tls.crt"
    assert options["ssl_keyfile"] == "/tls/tls.key"
    assert options["access_log"] is False and options["proxy_headers"] is False


@pytest.mark.parametrize("option", ["--tls-cert", "--tls-key"])
def test_partial_tls_configuration_fails_before_reading_credentials(monkeypatch, option):
    monkeypatch.setattr(registration_server, "runtime_connection", lambda: pytest.fail("credential read"))
    with pytest.raises(SystemExit) as error:
        registration_server.main([option, "/tls/file"])
    assert error.value.code == 2


def test_existing_tls_ingress_backend_keeps_8080(host):
    registration_server.main([])
    options = host[1][0][1]
    assert options["port"] == 8080 and "ssl_keyfile" not in options
