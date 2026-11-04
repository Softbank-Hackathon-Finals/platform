"""One bounded OCI import on the reviewed RKE2 node via an SSM loopback tunnel.

Input is a non-secret build receipt. Port 18981 listens only on loopback, accepts
one size/hash-bound PUT, and closes after 300 seconds. It changes no IAM, registry,
host configuration or existing container. Run as the authenticated operator.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    expected = json.load(sys.stdin)
    digest, size = expected["archive_sha256"], expected["archive_bytes"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or not 0 < size < 512 * 1024**2:
        raise ValueError("invalid_archive_receipt")
    result = {"imported": False}
    with tempfile.TemporaryDirectory(prefix="bravo-window-import-") as directory:
        archive = Path(directory) / "image.tar"

        class Upload(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_PUT(self):
                self.connection.settimeout(60)
                if self.path != "/" + digest or self.headers.get("Content-Length") != str(size):
                    self.send_error(400)
                    return
                hashed = hashlib.sha256()
                remaining = size
                with archive.open("xb") as stream:
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024**2))
                        if not chunk:
                            raise ValueError("incomplete_archive")
                        stream.write(chunk)
                        hashed.update(chunk)
                        remaining -= len(chunk)
                if hashed.hexdigest() != digest:
                    self.send_error(400)
                    return
                command = ["/var/lib/rancher/rke2/bin/ctr", "-a", "/run/k3s/containerd/containerd.sock",
                           "-n", "k8s.io", "images", "import", "--platform", "linux/amd64", str(archive)]
                imported = subprocess.run(command, capture_output=True, timeout=120)
                if imported.returncode:
                    self.send_error(500)
                    return
                result.update(imported=True, archive_sha256=digest, archive_bytes=size)
                self.send_response(201)
                self.end_headers()

        with HTTPServer(("127.0.0.1", 18981), Upload) as server:
            server.timeout = 300
            server.handle_request()
    result["temporary_archive_removed"] = not Path(directory).exists()
    print(json.dumps(result))
    if not result["imported"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
