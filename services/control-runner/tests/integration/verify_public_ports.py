"""Disposable Linux container only; actual Nmap on owned loopback listeners.

No team/public destination and no operational external-placement claim.
"""

import json
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from bravo_controls.public_ports import (
    SPEC,
    IngressReceipt,
    NmapTCPScanner,
    PublicPortsProbe,
    PublicScanTarget,
    ScanHost,
    digest,
)
from bravo_controls.runner import Environment, RunContext


class Handler(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            # TCP connect scanning closes before sending an HTTP request.
            pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"qualified-local-ingress")

    def log_message(self, *_args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    extra = socket.socket()
    extra.bind(("127.0.0.1", 0))
    env = Environment("local", "source", "qualification/loopback")
    target = PublicScanTarget(
        "qualification",
        env,
        "local-test-worker",
        (ScanHost("loopback", "127.0.0.1", frozenset({server.server_port})),),
        "local-http-control",
        "qualification-v1",
    )
    binary = Path("/usr/bin/nmap")
    scanner = NmapTCPScanner(binary, digest(binary.read_bytes()), "7.95")
    evidence = []
    opener = build_opener(ProxyHandler({}))

    def control(context, target, nonce):
        responded = False
        try:
            with opener.open(f"http://127.0.0.1:{server.server_port}", timeout=2) as response:
                responded = (
                    response.status == 200 and response.read(128) == b"qualified-local-ingress"
                )
        except (URLError, TimeoutError):
            responded = False
        return IngressReceipt(
            context,
            target.binding,
            nonce,
            target.external_agent,
            target.ingress_ref,
            responded,
            datetime.now(UTC),
        )

    probe = PublicPortsProbe(
        target,
        scanner=scanner,
        read_inventory=lambda *_: target,
        check_ingress=control,
        evidence_sink=evidence.append,
    )
    try:
        clean = probe(RunContext("clean", target.app, env), SPEC)
        assert clean.observed == "absent", "actual closed/open summary rejected"
        extra.listen(128)
        exposed = probe(RunContext("exposed", target.app, env), SPEC)
        assert exposed.observed == "present", "unexpected actual listener missed"
        actual = json.loads(evidence[-1])["results"][0]["unexpected_open_ports"]
        assert actual == [extra.getsockname()[1]]
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        unavailable = probe(RunContext("unavailable", target.app, env), SPEC)
        assert unavailable.unknown_reason == "POSITIVE_CONTROL_FAILED"
        print(
            json.dumps(
                {
                    "tool": "nmap",
                    "version": scanner.version,
                    "tool_sha256": scanner.sha256,
                    "tcp_ports_per_host": 65535,
                    "allowed_only": "PASS",
                    "extra_listener": "FAIL",
                    "ingress_unavailable": "UNKNOWN",
                    "operational_qualification": False,
                }
            )
        )
    finally:
        extra.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
