#!/usr/bin/env python3
"""Answer the app's one Secrets Manager call from the delivered Secret, on loopback only.

The unchanged image calls GetSecretValue at startup. This separate container lets
it start for development measurement without AWS. It is NOT evidence that the app
left AWS: the code dependency stays until the approved T15 change and T31 image
replace this container (docs/adr/0017-target-app-secrets-manager-compat.md).

Standard library only, so it runs from the same image. Nothing is logged: requests
carry a signature and responses carry the credential.
"""
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 18081
CREDENTIALS_FILE = "/run/bravo/database/credentials.json"
OPERATION = "secretsmanager.GetSecretValue"
MAX_REQUEST_BYTES = 64 * 1024


def read_credentials(path):
    """Return the file text only if it has the shape the app parses."""
    text = Path(path).read_text()
    value = json.loads(text)
    if not isinstance(value, dict) or not all(
            isinstance(value.get(key), str) and value[key] for key in ("username", "password")):
        raise ValueError("username and password are required")
    return text


def make_server(secret_id, credentials_path, port=PORT):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/x-amz-json-1.1")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def refuse(self):
            # The body may be unread here, so this connection cannot be reused.
            self.close_connection = True
            self.reply(400, {"__type": "ResourceNotFoundException", "Message": "unknown secret"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.refuse()
            if (self.path != "/" or not 0 < length <= MAX_REQUEST_BYTES
                    or self.headers.get("X-Amz-Target") != OPERATION):
                return self.refuse()
            try:
                request = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return self.refuse()
            if not isinstance(request, dict) or request.get("SecretId") != secret_id:
                return self.refuse()
            try:
                text = read_credentials(credentials_path)
            except (OSError, ValueError):
                return self.reply(500, {"__type": "InternalServiceError",
                                        "Message": "credentials unavailable"})
            self.reply(200, {"Name": secret_id, "SecretString": text})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    secret_id = os.environ["DB_SECRET_ID"]
    path = os.environ.get("BRAVO_COMPAT_CREDENTIALS", CREDENTIALS_FILE)
    try:
        read_credentials(path)
    except (OSError, ValueError):
        # Stop here so the app never starts on an unusable Secret. The message stays generic.
        sys.exit("database credentials are unavailable or malformed")
    # As process 1 this has no default SIGTERM action, and would hold every rollout for the grace period.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    make_server(secret_id, path, int(os.environ.get("BRAVO_COMPAT_PORT", PORT))).serve_forever()


if __name__ == "__main__":
    main()
