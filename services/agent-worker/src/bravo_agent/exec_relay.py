"""T38 exec relay: worker child -> broker (local socket) -> T15 (mTLS), contract T42 v1.

The worker process never holds the T15 client certificate or chooses run_id/attempt.
It sends T11's ``local_request`` over a private Unix socket. The broker checks the exact
field set, attaches run_id and attempt from the product input it is serving, and posts
``channel_request`` to the fixed T15 path. A relay or T15 failure closes the connection
without a response, so T11 applies its channel-failure rules (contract section 5).
"""

import http.client
import json
import os
import socket
import ssl
import tempfile
import threading
from pathlib import Path

LOCAL_FIELDS = {"schema_version", "request_id", "args_sha256", "tool", "arguments"}
MAX_BODY = 262144  # same bound as the T15 server
PATH = "/v1/rehearsal-exec"


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _read_all(connection, limit):
    data = b""
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            return data
        data += chunk
        if len(data) > limit:
            raise ValueError("oversized exec message")


class SocketExecChannel:
    """Worker side. ``enabled`` is True only because the broker created this socket for this run."""

    enabled = True

    def __init__(self, path, timeout=20.0):
        self.path, self.timeout = str(path), timeout

    def send(self, envelope: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            connection.sendall(json.dumps(envelope, separators=(",", ":")).encode())
            connection.shutdown(socket.SHUT_WR)
            data = _read_all(connection, MAX_BODY)
        if not data:
            raise RuntimeError("exec channel closed without a response")
        return json.loads(data)


def t15_forwarder(url, ca_file, cert_file, key_file, server_name, timeout=20.0):
    """mTLS POST to the T15 exec server; only the reviewed CA, no system roots."""
    host, _, port = url.removeprefix("https://").partition(":")
    if not url.startswith("https://") or not host or not port.isdigit():
        raise ValueError("exact https://host:port T15 exec address required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=ca_file)
    context.load_cert_chain(cert_file, key_file)

    def forward(request: dict) -> dict:
        body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        connection = http.client.HTTPSConnection(host, int(port), context=context, timeout=timeout)
        try:
            # Verify the runner certificate against its configured name, not the dialed address.
            connection.sock = context.wrap_socket(socket.create_connection((host, int(port)), timeout),
                                                  server_hostname=server_name)
            connection.request("POST", PATH, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(MAX_BODY + 1)
        finally:
            connection.close()
        if response.status != 200 or len(data) > MAX_BODY:
            raise RuntimeError("T15 exec path did not answer")  # 400/403/413/503 are channel failures
        return json.loads(data, object_pairs_hook=_no_duplicates)

    return forward


class ExecRelay:
    """Broker side, for the lifetime of one worker run."""

    def __init__(self, forward, run_id: str, attempt: int):
        self.forward, self.run_id, self.attempt = forward, run_id, attempt
        self.directory = Path(tempfile.mkdtemp(prefix="bravo-exec-"))  # 0700
        self.path = self.directory / "exec.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.stopping = threading.Event()

    def channel_request(self, raw: bytes) -> dict:
        local = json.loads(raw, object_pairs_hook=_no_duplicates)
        if type(local) is not dict or set(local) != LOCAL_FIELDS or local["schema_version"] != 1:
            raise ValueError("not a T42 v1 local_request")
        return {**local, "run_id": self.run_id, "attempt": self.attempt}

    def _serve_one(self, connection):
        with connection:
            try:
                connection.settimeout(30)
                response = self.forward(self.channel_request(_read_all(connection, MAX_BODY)))
                connection.sendall(json.dumps(response, separators=(",", ":")).encode())
            except Exception:
                pass  # No response: the worker treats it as a channel failure.

    def _loop(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self._serve_one(connection)  # one action at a time, like the T15 session

    def __enter__(self):
        old = os.umask(0o177)
        try:
            self.server.bind(str(self.path))
        finally:
            os.umask(old)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping.set()
        self.server.close()
        self.thread.join(timeout=5)
        self.path.unlink(missing_ok=True)
        self.directory.rmdir()
