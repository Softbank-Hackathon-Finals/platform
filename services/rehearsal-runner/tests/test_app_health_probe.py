import io
from urllib.error import HTTPError, URLError

import pytest

from rehearsal_runner.app_health_probe import observe_app_database


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, length):
        return self.body[:length]


class Opener:
    def __init__(self, result):
        self.result, self.requests = result, []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def probe(monkeypatch, result):
    opener = Opener(result)
    monkeypatch.setattr("rehearsal_runner.app_health_probe.build_opener", lambda *args: opener)
    return opener


def test_db_health_success(monkeypatch):
    opener = probe(monkeypatch, Response(b'{"ok":true}'))
    assert observe_app_database("rehearsal-x", "candidate", 8000) == "yes"
    assert opener.requests == [("http://candidate.rehearsal-x.svc.cluster.local:8000/api/health/db", 5)]


def test_known_db_unavailable_response(monkeypatch):
    response = HTTPError("http://example", 503, "error", {}, io.BytesIO(b'{"detail":"database unavailable"}'))
    probe(monkeypatch, response)
    assert observe_app_database("rehearsal-x", "candidate", 8000) == "no"


@pytest.mark.parametrize("result", [Response(b'{"ok":false}'), Response(b"a" * 1025),
                                    URLError("network"), HTTPError("http://example", 403, "error", {}, None)])
def test_unknown_responses_never_report_success(monkeypatch, result):
    probe(monkeypatch, result)
    assert observe_app_database("rehearsal-x", "candidate", 8000) == "unknown"


@pytest.mark.parametrize("namespace,service,port", [("../default", "candidate", 8000),
                                                     ("rehearsal-x", "elsewhere/path", 8000),
                                                     ("rehearsal-x", "candidate", True)])
def test_invalid_scope_rejected_before_request(monkeypatch, namespace, service, port):
    opener = probe(monkeypatch, Response(b'{"ok":true}'))
    with pytest.raises(ValueError):
        observe_app_database(namespace, service, port)
    assert opener.requests == []


def test_observer_uses_real_http_against_scoped_service_name(monkeypatch):
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/health/db":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    original = socket.getaddrinfo

    def fixture_dns(host, *args, **kwargs):
        if host == "candidate.rehearsal-x.svc.cluster.local":
            host = "127.0.0.1"
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fixture_dns)
    thread.start()
    try:
        assert observe_app_database("rehearsal-x", "candidate", server.server_port) == "yes"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
