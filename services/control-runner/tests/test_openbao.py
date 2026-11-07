import hashlib
import json
import ssl
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bravo_controls.openbao import (
    MAX_RESPONSE,
    BaoTarget,
    KubernetesPrincipal,
    OpenBaoClient,
    OpenBaoSecretAccessProbe,
)
from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 4, tzinfo=UTC)
ENV = Environment("fixture-target", "target", "registered/target")
TARGET = BaoTarget("demo-chat", ENV, "kv", "apps/demo-chat/db", 3)
CONTEXT = RunContext("fixture-run", "demo-chat", ENV)
SPEC = Spec("SEC-01.1", "SEC-01", "1", "deny", "required")
APP = KubernetesPrincipal("kubernetes", "app", "demo-chat", "uid-app", "app-role", "accessor-app")
OTHER = KubernetesPrincipal(
    "kubernetes", "other", "other-app", "uid-other", "other-role", "accessor-other"
)
CANARY = "DO-NOT-PERSIST-SECRET-CANARY"


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    root = tmp_path_factory.mktemp("bao-tls")
    crt, key = root / "ca.pem", root / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return crt, key


def identity(principal):
    return {
        "data": {
            "id": CANARY,
            "accessor": principal.accessor,
            "ttl": 120,
            "path": f"auth/{principal.auth_mount}/login",
            "policies": ["default", "app"],
            "meta": {
                "role": principal.role,
                "service_account_name": principal.service_account,
                "service_account_namespace": principal.namespace,
                "service_account_uid": principal.uid,
            },
        }
    }


def secret(version=3):
    return {
        "data": {
            "data": {"password": CANARY},
            "metadata": {"version": version, "destroyed": False, "deletion_time": ""},
        }
    }


def clients(cert, origin="https://localhost:8200"):
    ca = cert[0].read_text()
    return (
        OpenBaoClient(origin, ca, "fixture-app-token", APP),
        OpenBaoClient(origin, ca, "fixture-other-token", OTHER),
    )


def setup(cert, monkeypatch, alter=None, sink=None):
    app, other = clients(cert)
    calls = []
    retained = []

    def get(client, path):
        calls.append((client.principal, path))
        default = (
            (200, identity(client.principal))
            if path.endswith("lookup-self")
            else (
                (200, secret())
                if client.principal == APP
                else (403, {"errors": ["permission denied"]})
            )
        )
        return alter(client, path, calls, default) if alter else default

    monkeypatch.setattr(OpenBaoClient, "_get", get)
    probe = OpenBaoSecretAccessProbe(
        TARGET,
        app_client=app,
        other_client=other,
        evidence_sink=sink or retained.append,
        clock=lambda: NOW,
    )
    return probe, calls, retained


def test_complete_run_brackets_identity_and_current_secret(cert, monkeypatch, capsys):
    probe, calls, retained = setup(cert, monkeypatch)
    root = Path(__file__).resolve().parents[3] / "docs/controls"
    catalog = Catalog(
        json.loads((root / "profiles.json").read_text()),
        json.loads((root / "control-result.schema.json").read_text()),
    )
    run = Runner(catalog, {"SEC-01.1": probe}, clock=lambda: NOW).run(
        profile_id="demo-gate", profile_version="1", app="demo-chat", environment=ENV
    )
    assert [row["verdict"] for row in run["results"]] == ["UNKNOWN", "PASS", "UNKNOWN"]
    assert len(calls) == 7
    assert [p for _, p in calls].count(TARGET.read_path) == 3
    assert calls[2][0] == calls[-1][0] == APP
    assert calls[3] == (OTHER, TARGET.read_path)
    evidence = json.loads(retained[0])
    assert evidence["run_id"] == run["run_id"]
    assert evidence["steps"]["other_identity_after"]["matches"] is True
    obs = probe(CONTEXT, SPEC)
    assert obs.output_sha256 == "sha256:" + hashlib.sha256(retained[-1]).hexdigest()
    public = json.dumps(run) + b"".join(retained).decode() + capsys.readouterr().out
    for forbidden in (CANARY, "fixture-app-token", "accessor-app", "accessor-other", TARGET.path):
        assert forbidden not in public


def test_cross_app_readable_is_failure(cert, monkeypatch):
    def alter(client, path, calls, default):
        return (
            (200, secret()) if client.principal == OTHER and path == TARGET.read_path else default
        )

    probe, _, _ = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed == "allow" and result.positive_control == "pass"


@pytest.mark.parametrize(
    "field,value",
    [
        ("accessor", "wrong"),
        ("path", "auth/token/create"),
        ("ttl", 0),
        ("ttl", True),
        ("policies", ["root"]),
        ("policies", []),
        ("meta", {}),
        ("meta", None),
    ],
)
def test_wrong_or_expired_identity_stops_reads(cert, monkeypatch, field, value):
    def alter(client, path, calls, default):
        if client.principal == OTHER:
            response = identity(OTHER)
            response["data"][field] = value
            return 200, response
        return default

    probe, calls, _ = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed is None
    assert all(path.endswith("lookup-self") for _, path in calls)


def test_revoked_after_403_cannot_pass(cert, monkeypatch):
    def alter(client, path, calls, default):
        if len(calls) == 6:
            return 403, {"errors": ["permission denied"]}
        return default

    probe, _, _ = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == "OBSERVATION_DENIED"


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {}),
        (403, {}),
        (403, {"errors": ["invalid token"]}),
        (404, {}),
        (503, {}),
        (200, {"data": None}),
        (200, secret(4)),
        (200, secret(True)),
    ],
)
def test_unknown_negative_response_never_passes(cert, monkeypatch, status, body):
    def alter(client, path, calls, default):
        return (status, body) if client.principal == OTHER and path == TARGET.read_path else default

    probe, _, _ = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"


@pytest.mark.parametrize("when", [3, 7])
def test_missing_or_rotated_app_secret_blocks(cert, monkeypatch, when):
    def alter(client, path, calls, default):
        return (200, secret(4)) if len(calls) == when else default

    probe, calls, _ = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed is None and result.positive_control == "fail"
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    if when == 3:
        assert (OTHER, TARGET.read_path) not in calls


@pytest.mark.parametrize(
    "error,reason", [(TimeoutError(CANARY), "TIMEOUT"), (OSError(CANARY), "TOOL_ERROR")]
)
def test_transport_errors_are_unknown_without_error_text(cert, monkeypatch, error, reason):
    def alter(client, path, calls, default):
        if client.principal == OTHER and path == TARGET.read_path:
            raise error
        return default

    probe, _, retained = setup(cert, monkeypatch, alter)
    result = probe(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == reason
    assert CANARY not in retained[0].decode()


def test_evidence_retention_failure_cannot_return_pass(cert, monkeypatch):
    def sink(_):
        raise OSError(CANARY)

    probe, _, _ = setup(cert, monkeypatch, sink=sink)
    with pytest.raises(ValueError, match="evidence retention failed") as e:
        probe(CONTEXT, SPEC)
    assert CANARY not in str(e.value)


@pytest.mark.parametrize(
    "spec",
    [
        replace(SPEC, test_id="IAM-01.1"),
        replace(SPEC, method_version="2"),
        replace(SPEC, positive_control="not_applicable"),
    ],
)
def test_contract_mismatch_rejected_before_io(cert, monkeypatch, spec):
    probe, calls, _ = setup(cert, monkeypatch)
    with pytest.raises(ValueError):
        probe(CONTEXT, spec)
    assert not calls


@pytest.mark.parametrize(
    "path", ["../admin", "/app", "app//db", "app/..", "app?x=1", "app%2Fdb", "app#db", "app\n"]
)
def test_path_injection_rejected(path):
    with pytest.raises(ValueError):
        replace(TARGET, path=path)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "https://x/path",
        "https://x?query",
        "https://u:p@x",
        "https://x/#f",
        "https://x:99999",
    ],
)
def test_unsafe_origin_rejected(cert, origin):
    with pytest.raises(ValueError):
        clients(cert, origin)


def test_independent_identity_and_environment_required(cert):
    app, other = clients(cert)
    for bad in (
        app,
        replace(other, token=app.token),
        replace(other, principal=APP),
        replace(other, origin="https://elsewhere"),
    ):
        with pytest.raises(ValueError):
            OpenBaoSecretAccessProbe(
                TARGET, app_client=app, other_client=bad, evidence_sink=lambda _: None
            )


@pytest.fixture
def tls_server(cert):
    requests = []
    config = {"reply": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("X-Vault-Token")))
            status, body, headers = config["reply"](self.path, self.headers)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ssl.SSLError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert[0]), str(cert[1]))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", config, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_tls_transport_to_t5_observation(cert, tls_server, monkeypatch):
    origin, config, requests = tls_server
    app, other = clients(cert, origin)

    def reply(path, headers):
        p = APP if headers.get("X-Vault-Token") == app.token else OTHER
        if path.endswith("lookup-self"):
            status, body = 200, identity(p)
        elif p == APP:
            status, body = 200, secret()
        else:
            status, body = 403, {"errors": ["permission denied"]}
        return status, json.dumps(body).encode(), {"Content-Type": "application/json"}

    config["reply"] = reply
    # The collector intentionally ignores ambient HTTP proxies.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    retained = []
    probe = OpenBaoSecretAccessProbe(
        TARGET, app_client=app, other_client=other, evidence_sink=retained.append, clock=lambda: NOW
    )
    assert probe(CONTEXT, SPEC).observed == "deny"
    assert len(requests) == 7 and CANARY not in retained[0].decode()


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (200, b'{"data":{},"data":{}}', {"Content-Type": "application/json"}),
        (200, b'{"data": NaN}', {"Content-Type": "application/json"}),
        (200, b"x" * (MAX_RESPONSE + 1), {"Content-Type": "application/json"}),
        (200, b"[]", {"Content-Type": "application/json"}),
        (200, b"{}", {"Content-Type": "text/html"}),
        (200, b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}),
    ],
)
def test_real_transport_rejects_ambiguous_payloads(cert, tls_server, status, body, headers):
    origin, config, _ = tls_server
    config["reply"] = lambda *_: (status, body, headers)
    app, _ = clients(cert, origin)
    with pytest.raises(ValueError, match="OpenBao read failed"):
        app.identity_matches()


def test_redirect_not_followed_or_authenticated(cert, tls_server):
    origin, config, requests = tls_server
    config["reply"] = lambda *_: (307, b"", {"Location": origin + "/other"})
    app, _ = clients(cert, origin)
    assert app.identity_matches() is False
    assert len(requests) == 1


def test_untrusted_ca_never_sends_token(cert, tls_server, tmp_path):
    origin, config, requests = tls_server
    config["reply"] = lambda *_: (200, b"{}", {"Content-Type": "application/json"})
    other_ca, key = tmp_path / "other.pem", tmp_path / "other.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(other_ca),
            "-subj",
            "/CN=wrong-ca",
        ],
        check=True,
        capture_output=True,
    )
    app, _ = clients(cert, origin)
    with pytest.raises(ValueError, match="OpenBao read failed"):
        replace(app, ca_pem=other_ca.read_text()).identity_matches()
    assert not requests


def test_hostname_mismatch_never_sends_token(cert, tls_server):
    origin, config, requests = tls_server
    config["reply"] = lambda *_: (200, b"{}", {"Content-Type": "application/json"})
    app, _ = clients(cert, origin.replace("localhost", "127.0.0.1"))
    with pytest.raises(ValueError, match="OpenBao read failed"):
        app.identity_matches()
    assert not requests


def test_a_port_forward_verifies_the_registered_server_name(cert, tls_server):
    origin, config, requests = tls_server
    config["reply"] = lambda path, headers: (
        200, json.dumps(identity(APP)).encode(), {"Content-Type": "application/json"})
    forwarded = origin.replace("localhost", "127.0.0.1")  # the loopback end of kubectl port-forward
    app = replace(clients(cert, forwarded)[0], server_hostname="localhost")  # the name the cert carries
    assert app.identity_matches() is True and len(requests) == 1
    with pytest.raises(ValueError, match="OpenBao read failed"):
        replace(app, server_hostname="target-openbao.bravo-secrets.svc").identity_matches()
    assert len(requests) == 1  # a name the certificate does not carry never receives the token


@pytest.mark.parametrize(("origin", "name"), [
    ("https://openbao.example.invalid:8200", "localhost"),   # a name override only for a port-forward
    ("https://127.0.0.1:8200", "Target-OpenBao"),
    ("https://127.0.0.1:8200", "a/b"),
])
def test_a_server_name_needs_a_loopback_origin_and_a_dns_name(cert, origin, name):
    with pytest.raises(ValueError):
        OpenBaoClient(origin, cert[0].read_text(), "fixture-app-token", APP, server_hostname=name)


def test_both_clients_use_the_same_server_name(cert):
    app, other = clients(cert, "https://127.0.0.1:8200")
    with pytest.raises(ValueError, match="same server"):
        OpenBaoSecretAccessProbe(TARGET, app_client=replace(app, server_hostname="localhost"), other_client=other,
                                 evidence_sink=lambda _: None)

