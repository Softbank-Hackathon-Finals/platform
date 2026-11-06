"""Check this process over loopback; validate server identity when TLS is set."""

import http.client
import json
import os
import socket
import ssl


def check() -> None:
    directory = os.environ.get("BRAVO_API_SERVER_TLS_DIR")
    port = int(os.environ.get("PORT", "8080"))
    context = None
    if directory is not None:
        if not directory or not os.path.isabs(directory):
            raise ValueError("absolute API server TLS directory required")
        name = os.environ["BRAVO_API_SERVER_NAME"]
        if not name or any(ord(c) < 33 or ord(c) > 126 for c in name):
            raise ValueError("API server name required")
        context = ssl.create_default_context(cafile=directory + "/ca.crt")
        context.minimum_version = ssl.TLSVersion.TLSv1_3
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        connection = context.wrap_socket(raw, server_hostname=name) if context else raw
        with connection:
            connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = http.client.HTTPResponse(connection)
            response.begin()
            body = response.read(1025)
            if response.status != 200 or len(body) > 1024 or json.loads(body) != {"status": "ok"}:
                raise ValueError("API health unavailable")


if __name__ == "__main__":
    check()
