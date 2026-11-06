import http.client
import json
import shutil
import ssl
import subprocess

import pytest
from exec_schema import assert_valid

from rehearsal_runner.exec_contract import args_sha256, inspect_record
from rehearsal_runner.exec_flow import ExecRegistry
from rehearsal_runner.exec_server import (
    MAX_BODY,
    PATH,
    ExecServer,
    handle_bytes,
    server_tls_context,
    subject_common_name,
)
from rehearsal_runner.exec_session import ExecSession, Outcome, Space

STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
          "ready_replicas": None, "restarts": 0, "events": None}
POD = {"target_kind": "pod", "target_name": "chat", "lines": None}


class Inspect:
    def validate(self, arguments):
        return inspect_record(arguments)

    def run(self, arguments, deadline):
        return Outcome(STATUS)


def registry():
    r = ExecRegistry()
    r.register(ExecSession("run-1", 1, Space("rehearsal-a1", "uid-a"), {"inspect": Inspect()},
                           secrets=lambda: [], space_uid=lambda: "uid-a"))
    return r


def body(request_id="req-0001-srv", **override):
    raw = {"schema_version": 1, "run_id": "run-1", "attempt": 1, "request_id": request_id,
           "args_sha256": args_sha256(POD), "tool": "rehearsal.exec.inspect", "arguments": POD}
    return json.dumps(raw | override).encode()


def test_handle_bytes_maps_contract_and_channel_errors():
    r = registry()
    status, data = handle_bytes(r, body())
    assert status == 200
    assert_valid("response", json.loads(data))
    assert handle_bytes(r, b"\xff")[0] == 400
    assert handle_bytes(r, b'{"tool":"x","tool":"y"}')[0] == 400  # duplicate keys
    assert handle_bytes(r, body(request_id="short"))[0] == 400

    class Broken:
        def handle(self, raw):
            raise RuntimeError("store down")

    assert handle_bytes(Broken(), body()) == (503, b'{"error":"UNAVAILABLE"}')


# -- real TLS on loopback ------------------------------------------------------------------

def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def issue(directory, name, ca, cn, *, server=False):
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={cn}", cwd=directory)
    ext = directory / f"{name}.ext"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n" if server else "extendedKeyUsage=clientAuth\n")
    openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key",
            "-CAcreateserial", "-out", f"{name}.crt", "-days", "1", "-extfile", ext.name, cwd=directory)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("pki")
    for ca in ("server-ca", "broker-ca", "other-ca"):
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{ca}.key", "-out", f"{ca}.crt",
                "-days", "1", "-subj", f"/CN={ca}", cwd=d)
    issue(d, "server", "server-ca", "rehearsal-runner", server=True)
    issue(d, "broker", "broker-ca", "bravo-broker")
    issue(d, "impostor", "broker-ca", "someone-else")
    issue(d, "rogue", "other-ca", "bravo-broker")
    return d


@pytest.fixture
def server(pki):
    tls = server_tls_context(str(pki / "server.crt"), str(pki / "server.key"), str(pki / "broker-ca.crt"))
    s = ExecServer(registry(), ("127.0.0.1", 0), tls, subject_common_name("bravo-broker"))
    s.start()
    yield s
    s.stop()


def post(server, pki, client, payload, *, method="POST", path=PATH, content_type="application/json"):
    tls = ssl.create_default_context(cafile=str(pki / "server-ca.crt"))
    if client:
        tls.load_cert_chain(str(pki / f"{client}.crt"), str(pki / f"{client}.key"))
    conn = http.client.HTTPSConnection("localhost", server.address[1], context=tls, timeout=10)
    try:
        conn.request(method, path, body=payload, headers={"Content-Type": content_type})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_broker_certificate_reaches_the_session(server, pki):
    status, data = post(server, pki, "broker", body())
    assert status == 200
    response = json.loads(data)
    assert (response["result_code"], response["request_id"]) == ("OK", "req-0001-srv")
    assert_valid("response", response)


@pytest.mark.parametrize("client", [None, "rogue"])
def test_without_a_broker_ca_certificate_the_handshake_fails(server, pki, client):
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        post(server, pki, client, body())


def test_other_identity_from_the_broker_ca_is_forbidden(server, pki):
    assert post(server, pki, "impostor", body())[0] == 403


@pytest.mark.parametrize("kwargs,status", [
    ({"path": "/other"}, 404),
    ({"method": "GET"}, 405),
    ({"content_type": "text/plain"}, 415),
])
def test_only_the_exec_route_accepts_json(server, pki, kwargs, status):
    assert post(server, pki, "broker", body(), **kwargs)[0] == status


def test_oversized_body_is_refused_before_reading(server, pki):
    tls = ssl.create_default_context(cafile=str(pki / "server-ca.crt"))
    tls.load_cert_chain(str(pki / "broker.crt"), str(pki / "broker.key"))
    conn = http.client.HTTPSConnection("localhost", server.address[1], context=tls, timeout=10)
    try:  # announce a large body but send none: the server answers from the header alone
        conn.putrequest("POST", PATH)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
    finally:
        conn.close()


def test_server_refuses_a_context_without_client_verification(pki):
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(pki / "server.crt"), str(pki / "server.key"))
    with pytest.raises(ValueError):
        ExecServer(registry(), ("127.0.0.1", 0), tls, subject_common_name("bravo-broker"))
