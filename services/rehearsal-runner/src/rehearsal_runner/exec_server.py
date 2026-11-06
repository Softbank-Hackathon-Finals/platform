"""T15 receiving end of the rehearsal.exec channel: T38 broker -> fixed private TLS path.

Transport only. The broker's TLS identity is verified by the server context and
an injected `authorize` check; the request body goes to `ExecRegistry.handle`,
which binds run, attempt and space A on the server side. Nothing here comes
from the model, and request bodies are never logged.

    POST /v1/rehearsal-exec   body: contract v1 channel_request   ->   200 response
    400 MALFORMED (unreadable request), 403 (caller refused), 413 (too large),
    503 UNAVAILABLE (T15 could not answer; T11 treats it as a channel failure)
"""

import json
import ssl
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .exec_contract import MalformedRequest

PATH = "/v1/rehearsal-exec"
MAX_BODY = 262144  # an apply draft is at most 4 x 32 KiB of file text plus its envelope


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def handle_bytes(registry, body: bytes) -> tuple[int, bytes]:
    """Map one request body to (HTTP status, response body); never raises."""
    try:
        raw = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return 400, _encode({"error": "MALFORMED"})
    try:
        return 200, _encode(registry.handle(raw))
    except MalformedRequest:
        return 400, _encode({"error": "MALFORMED"})
    except Exception:
        return 503, _encode({"error": "UNAVAILABLE"})


def server_tls_context(cert_file: str, key_file: str, broker_ca_file: str) -> ssl.SSLContext:
    """Server certificate plus a required client certificate from the reviewed broker CA only."""
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert_file, key_file)
    tls.load_verify_locations(cafile=broker_ca_file)  # no system roots
    tls.verify_mode = ssl.CERT_REQUIRED
    return tls


def subject_common_name(expected: str) -> Callable[[dict], bool]:
    """`authorize` that accepts exactly one broker certificate common name."""

    def authorize(certificate: dict) -> bool:
        names = [value for rdn in certificate.get("subject", ()) for key, value in rdn if key == "commonName"]
        return names == [expected]

    return authorize


class ExecServer:
    def __init__(self, registry, address: tuple[str, int], tls: ssl.SSLContext,
                 authorize: Callable[[dict], bool], *, request_timeout: float = 30):
        if tls.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("the broker must present a client certificate")
        handler = _handler(registry, authorize, request_timeout)
        self.httpd = ThreadingHTTPServer(address, handler, bind_and_activate=False)
        self.httpd.socket = tls.wrap_socket(self.httpd.socket, server_side=True)
        self.httpd.daemon_threads = True
        self.httpd.server_bind()
        self.httpd.server_activate()
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self.httpd.server_address[:2]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)


def _handler(registry, authorize, request_timeout):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = request_timeout

        def log_message(self, *args):  # no paths, peers or bodies in logs
            pass

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self):
            if self.path != PATH:
                return self._reply(404, _encode({"error": "NOT_FOUND"}))
            try:
                allowed = authorize(self.connection.getpeercert() or {})
            except Exception:
                allowed = False
            if not allowed:
                return self._reply(403, _encode({"error": "FORBIDDEN"}))
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                return self._reply(411, _encode({"error": "LENGTH_REQUIRED"}))
            if int(length) > MAX_BODY:
                return self._reply(413, _encode({"error": "TOO_LARGE"}))
            if self.headers.get_content_type() != "application/json":
                return self._reply(415, _encode({"error": "UNSUPPORTED_MEDIA_TYPE"}))
            status, body = handle_bytes(registry, self.rfile.read(int(length)))
            self._reply(status, body)

        def _refuse(self):
            self._reply(405, _encode({"error": "METHOD_NOT_ALLOWED"}))

        do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _refuse

    return Handler
