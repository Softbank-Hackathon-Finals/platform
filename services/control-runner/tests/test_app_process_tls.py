import json
import socket
import ssl
import struct
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bravo_controls import Environment, RunContext
from bravo_controls.app_process_tls import (
    CLIENT,
    AppProcessRegistration,
    AppProcessTLSProbe,
    _wire,
    digest,
    encoded,
)
from bravo_controls.postgres_tls import PoolerTarget
from bravo_controls.runner import TestSpec as Spec

ENV = Environment("source", "source", "aws:eks/fixture")
CTX = RunContext("app-tls-fixture", "demo-chat", ENV)
CA = Path(__file__).parent / "fixtures/tls"
OK = {"outcome": "allow", "reason": None}
DENY = {"outcome": "deny", "reason": None}
UNKNOWN = {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}


def spec(test):
    return Spec(test, "ENC-01", "1", "success" if test == "ENC-01.1" else "deny",
                "not_applicable" if test == "ENC-01.1" else "required")


@pytest.fixture
def setup():
    target = PoolerTarget("demo-chat", ENV, "source-dev", "app-pod", "pooler-uid",
                          "sha256:" + "a" * 64, "pooler.test", "127.0.0.1", 5432,
                          "chat", "app", (CA / "ca.pem").read_bytes(),
                          (CA / "wrong-ca.pem").read_bytes())
    executable = Path(sys.executable)
    reg = AppProcessRegistration(target, "deployment-uid", "sha256:" + "b" * 64,
                                 "sha256:" + "c" * 64, "sha256:" + "d" * 64,
                                 executable, digest(executable.read_bytes()), digest(CLIENT.read_bytes()),
                                 "3.3.7", 180006)
    evidence = []

    def sink(payload):
        evidence.append(payload)
        return {"run_id": CTX.run_id, "output_sha256": digest(payload)}

    probe = AppProcessTLSProbe(reg, credential_provider=lambda ctx: "never-persist-app-secret",
                               binding_reader=lambda ctx: digest(encoded(reg.binding())),
                               evidence_sink=sink, clock=lambda: datetime.now(UTC))
    return probe, evidence


@pytest.mark.parametrize("test,negative,observed", [
    ("ENC-01.1", [], "success"), ("ENC-01.2", [DENY, DENY], "deny"),
    ("ENC-01.3", [DENY], "deny"), ("ENC-01.2", [OK, DENY], "allow"),
    ("ENC-01.3", [OK], "allow"), ("ENC-01.2", [DENY, UNKNOWN], None),
])
def test_normalized_results_and_secrets_stay_outside_evidence(setup, test, negative, observed, monkeypatch):
    probe, evidence = setup
    monkeypatch.setenv("PGSERVICE", "attacker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-pass-to-db-client")
    with patch("bravo_controls.app_process_tls._execute", return_value=(0, encoded(OK), b"")) as execute, \
            patch("bravo_controls.app_process_tls._wire", side_effect=negative):
        result = probe(CTX, spec(test))
    assert result.observed == observed
    assert result.tool == "bravo-app-process-tls"
    assert len(evidence) == 1
    assert not any(x in evidence[0] for x in (b"never-persist", b"pooler.test", b"127.0.0.1"))
    assert json.loads(evidence[0])["run_id"] == CTX.run_id
    for call in execute.call_args_list:
        assert "never-persist" not in repr(call.args)
        assert "PGSERVICE" not in call.kwargs["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in call.kwargs["env"]
        assert call.args[0][1] == "-I"
        config = json.loads(call.kwargs["env"]["BRAVO_TLS_CONFIG"])
        assert "sql" not in config


@pytest.mark.parametrize("which", ["before", "after"])
def test_live_binding_change_never_returns_observation(setup, which):
    probe, evidence = setup
    actual = digest(encoded(probe.registration.binding()))
    with patch.object(probe, "readback", side_effect=["changed", actual] if which == "before" else [actual, "changed"]), \
            patch("bravo_controls.app_process_tls._execute", return_value=(0, encoded(OK), b"")):
        with pytest.raises(ValueError, match="binding"):
            probe(CTX, spec("ENC-01.1"))
    assert evidence == []


@pytest.mark.parametrize("receipt", [None, {}, {"run_id": "another-run", "output_sha256": "sha256:" + "a" * 64}])
def test_retention_ack_is_required(setup, receipt):
    probe, _ = setup
    with patch.object(probe, "sink", return_value=receipt), \
            patch("bravo_controls.app_process_tls._execute", return_value=(0, encoded(OK), b"")):
        with pytest.raises(ValueError, match="acknowledgement"):
            probe(CTX, spec("ENC-01.1"))


def test_failed_after_positive_invalidates_negative_evidence(setup):
    probe, _ = setup
    with patch("bravo_controls.app_process_tls._execute", side_effect=[(0, encoded(OK), b""), TimeoutError()]), \
            patch("bravo_controls.app_process_tls._wire", return_value=DENY):
        result = probe(CTX, spec("ENC-01.3"))
    assert result.observed is None
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"


def test_wrong_client_digest_stops_before_credentials(setup):
    probe, _ = setup
    probe.registration = replace(probe.registration, client_sha256="sha256:" + "0" * 64)
    probe.readback = lambda ctx: digest(encoded(probe.registration.binding()))
    with patch.object(probe, "credentials") as credentials:
        with pytest.raises(ValueError, match="unqualified"):
            probe(CTX, spec("ENC-01.1"))
        credentials.assert_not_called()


class RawSocket:
    def __init__(self, data):
        self.data, self.sent = data, b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sendall(self, value):
        self.sent += value

    def settimeout(self, value):
        pass

    def recv(self, count):
        value, self.data = self.data[:count], self.data[count:]
        return value


@pytest.mark.parametrize("body,kind,outcome", [
    (b"SFATAL\0C08P01\0MSSL required\0\0", b"E", "deny"),
    (b"SFATAL\0C28P01\0Mwrong password\0\0", b"E", None),
    (b"C08P01\0C08P01\0MSSL required\0\0", b"E", None),
    (struct.pack("!I", 3), b"R", "allow"),
])
def test_plaintext_never_sends_password_and_only_exact_tls_refusal_counts(setup, body, kind, outcome):
    probe, _ = setup
    raw = RawSocket(kind + struct.pack("!I", len(body) + 4) + body)
    with patch.object(socket, "create_connection", return_value=raw):
        result = _wire(probe.registration.target, "plain")
    assert result["outcome"] == outcome
    assert raw.sent[4:8] == struct.pack("!I", 196608)
    assert b"password" not in raw.sent
    assert b"never-persist" not in raw.sent


@pytest.mark.parametrize("mode,verify_code,outcome", [
    ("wrong_ca", 20, "deny"), ("wrong_host", 62, "deny"),
    ("wrong_ca", 10, None), ("wrong_host", 20, None),
])
def test_certificate_error_must_match_the_control(setup, mode, verify_code, outcome):
    probe, _ = setup
    error = ssl.SSLCertVerificationError()
    error.verify_code = verify_code
    raw = RawSocket(b"S")
    with patch.object(socket, "create_connection", return_value=raw), \
            patch.object(ssl, "SSLContext") as context:
        context.return_value.wrap_socket.side_effect = error
        result = _wire(probe.registration.target, mode)
    assert result["outcome"] == outcome
    assert raw.sent == struct.pack("!II", 8, 80877103)
