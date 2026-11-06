"""Sandbox session VM Docker TLS: the broker client certificate is required (exec_pki.issue_docker)."""

import importlib.util
from pathlib import Path

import pytest


def _pki_module():
    path = Path(__file__).resolve().parents[3] / "operations/subscription/exec_pki.py"
    spec = importlib.util.spec_from_file_location("exec_pki", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sandbox_docker_tls_set_requires_the_broker_client_certificate(tmp_path):
    import socket
    import ssl
    import threading

    d = tmp_path / "docker-tls"
    summary = _pki_module().issue_docker(d, ["127.0.0.1"], days=1)
    assert set(summary["sha256"]) == {"ca.pem", "server-cert.pem", "cert.pem"}
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(d / "server-cert.pem", d / "server-key.pem")
    server.load_verify_locations(d / "ca.pem")
    server.verify_mode = ssl.CERT_REQUIRED  # dockerd tlsverify
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    seen = []

    def accept():
        for _ in range(2):
            connection, _ = listener.accept()
            try:
                with server.wrap_socket(connection, server_side=True) as tls:
                    seen.append(tls.getpeercert()["subject"])
            except ssl.SSLError:
                seen.append(None)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()

    def connect(with_client):
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.load_verify_locations(d / "ca.pem")
        if with_client:
            client.load_cert_chain(d / "cert.pem", d / "key.pem")
        with socket.create_connection(("127.0.0.1", port)) as raw, \
                client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
            tls.sendall(b"x")
            tls.recv(1)

    connect(True)  # the broker's DOCKER_CERT_PATH layout: ca.pem, cert.pem, key.pem
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        connect(False)
    thread.join(5)
    listener.close()
    assert seen[0] == ((("commonName", "bravo-broker"),),) and seen[1] is None
