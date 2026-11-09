import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bravo_controls import Catalog, Environment, RunContext, Runner
from bravo_controls.postgres_tls import PoolerTarget, PoolerTLSProbe, _execute, _result
from bravo_controls.runner import TestSpec as Spec

ENV = Environment("local", "rehearsal", "k8s:local/test")
CTX = RunContext("tls-test", "demo-chat", ENV)
NOW = datetime(2026, 11, 3, 11, tzinfo=UTC)
CA = Path(__file__).parent / "fixtures/tls"
OK = (
    0,
    b'You are connected to database "chat" as user "app".\nSSL Connection|true\nSSL Protocol|TLSv1.3\nSSL Cipher|TLS_AES_256_GCM_SHA384\nSSL Compression|false\n1\n',
    b"",
)


def deny(detail):
    return (
        2,
        b"",
        (
            'psql: error: connection to server at "pooler.test" (192.0.2.1), port 5432 failed: '
            + detail
            + "\n"
        ).encode(),
    )


CA_DENIED = deny("SSL error: certificate verify failed")
HOST_DENIED = deny(
    'server certificate for "pooler.test" does not match host name "wrong-pooler.invalid"'
)
PLAIN_DENIED = deny("FATAL:  SSL required")


def spec(test):
    return Spec(
        test,
        "ENC-01",
        "1",
        "success" if test == "ENC-01.1" else "deny",
        "not_applicable" if test == "ENC-01.1" else "required",
    )


@pytest.fixture
def setup(tmp_path):
    target = PoolerTarget(
        "demo-chat",
        ENV,
        "app-test",
        "pod-uid",
        "pooler-uid",
        "sha256:" + "a" * 64,
        "pooler.test",
        "192.0.2.1",
        5432,
        "chat",
        "app",
        (CA / "ca.pem").read_bytes(),
        (CA / "wrong-ca.pem").read_bytes(),
    )
    executable = tmp_path / "psql"
    executable.write_bytes(b"unit-test only")
    evidence = []
    probe = PoolerTLSProbe(
        target,
        executable=executable,
        executable_sha256="sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
        psql_version="18.6",
        credential_provider=lambda ctx: "never-persist-credential",
        evidence_sink=evidence.append,
        clock=lambda: NOW,
    )
    return probe, evidence


def invoke(probe, test, responses):
    with patch(
        "bravo_controls.postgres_tls._execute",
        side_effect=[(0, b"psql (PostgreSQL) 18.6\n", b""), *responses],
    ) as execute:
        result = probe(CTX, spec(test))
    return result, execute


@pytest.mark.parametrize(
    "test,responses,observed,positive",
    [
        ("ENC-01.1", [OK], "success", "not_applicable"),
        ("ENC-01.2", [OK, CA_DENIED, HOST_DENIED, OK], "deny", "pass"),
        ("ENC-01.3", [OK, PLAIN_DENIED, OK], "deny", "pass"),
    ],
)
def test_qualified_results_and_private_evidence(
    setup, test, responses, observed, positive, monkeypatch
):
    probe, evidence = setup
    monkeypatch.setenv("PGSERVICE", "attacker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-forwarded")
    result, execute = invoke(probe, test, responses)
    assert (result.observed, result.positive_control, result.unknown_reason) == (
        observed,
        positive,
        None,
    )
    assert result.output_sha256 == "sha256:" + hashlib.sha256(evidence[0]).hexdigest()
    payload = json.loads(evidence[0])
    assert payload["run_id"] == CTX.run_id
    assert len(payload["checks"]) == len(responses)
    assert not any(
        x in evidence[0] for x in (b"never-persist", b"192.0.2.1", b"pooler.test", b"as user")
    )
    for call in execute.call_args_list[1:]:
        env = call.kwargs["env"]
        assert "PGSERVICE" not in env and "AWS_SECRET_ACCESS_KEY" not in env
        assert env["PGHOSTADDR"] == "192.0.2.1"
        assert env["PGREQUIREAUTH"] == "scram-sha-256"
        assert env["PGGSSENCMODE"] == "disable"
        assert env["PGCONNECT_TIMEOUT"] == "5"
        assert "never-persist-credential" not in repr(call.args)
        assert call.args[0][-1] == "SELECT 1;"


@pytest.mark.parametrize(
    "responses,observed,reason,positive",
    [
        ([OK, OK, HOST_DENIED, OK], "allow", None, "pass"),
        ([OK, CA_DENIED, OK, OK], "allow", None, "pass"),
        ([OK, CA_DENIED, deny("timeout expired"), OK], None, "TOOL_ERROR", "pass"),
        (
            [OK, CA_DENIED, HOST_DENIED, deny("connection refused")],
            None,
            "POSITIVE_CONTROL_FAILED",
            "fail",
        ),
        ([deny("password authentication failed")], None, "POSITIVE_CONTROL_FAILED", "fail"),
        ([TimeoutError()], None, "POSITIVE_CONTROL_FAILED", "fail"),
    ],
)
def test_negative_or_failed_control_never_fabricates_pass(
    setup, responses, observed, reason, positive
):
    result, _ = invoke(setup[0], "ENC-01.2", responses)
    assert (result.observed, result.unknown_reason, result.positive_control) == (
        observed,
        reason,
        positive,
    )


@pytest.mark.parametrize(
    "mode,result",
    [
        ("wrong_ca", deny("root certificate file does not exist")),
        ("wrong_ca", deny("Connection refused")),
        ("wrong_ca", deny("FATAL:  password authentication failed")),
        ("wrong_ca", deny("FATAL:  SSL error: certificate verify failed")),
        ("wrong_host", deny('FATAL:  server certificate for "x" does not match host name "y"')),
        ("wrong_host", CA_DENIED),
        ("plain", deny("FATAL:  password authentication failed for user")),
        ("plain", deny("server closed the connection unexpectedly")),
        ("plain", deny("FATAL:  no pg_hba.conf entry for host x, SSL encryption")),
        ("plain", (1, b"", PLAIN_DENIED[2])),
        ("plain", (2, b"unexpected", PLAIN_DENIED[2])),
        ("positive", (0, b"1\n", b"")),
        ("positive", (0, OK[1].replace(b"TLSv1.3", b"TLSv1.1"), b"")),
        ("positive", (0, OK[1] + b"1\n", b"")),
        ("positive", (0, OK[1] + b"SSL Protocol|TLSv1.3\n", b"")),
        ("wrong_ca", (2, b"", CA_DENIED[2] + b"another failure\n")),
    ],
)
def test_unqualified_errors_and_tls_shapes_are_unknown(mode, result):
    assert _result(mode, *result)["outcome"] is None


def test_plain_hba_denial_and_positive_plain_connection():
    assert (
        _result(
            "plain",
            *deny(
                'FATAL:  no pg_hba.conf entry for host "192.0.2.1", user "app", database "chat", no encryption'
            ),
        )["outcome"]
        == "deny"
    )
    assert _result("plain", 0, b"1\n", b"")["outcome"] == "allow"


@pytest.mark.parametrize(
    "mode,detail",
    [
        ("wrong_ca", "SSL error: certificate verify failed"),
        (
            "wrong_host",
            'server certificate for "pooler" (and 11 other names) does not match host name "wrong-pooler.invalid"',
        ),
        ("plain", "FATAL:  SSL required"),
    ],
)
def test_linux_numeric_address_and_multi_san_error(mode, detail):
    error = (
        f'psql: error: connection to server at "192.0.2.1", port 5432 failed: {detail}\n'.encode()
    )
    assert _result(mode, 2, b"", error)["outcome"] == "deny"


def test_plain_acceptance_is_failure_and_missing_denial_is_unknown(setup):
    probe, _ = setup
    result, _ = invoke(probe, "ENC-01.3", [OK, (0, b"1\n", b""), OK])
    assert result.observed == "allow" and result.positive_control == "pass"
    result, _ = invoke(probe, "ENC-01.3", [OK, TimeoutError(), OK])
    assert result.observed is None and result.unknown_reason == "TIMEOUT"


def test_wrong_host_uses_same_literal_address_and_only_ca_changes_for_wrong_ca(setup):
    result, execute = invoke(setup[0], "ENC-01.2", [OK, CA_DENIED, HOST_DENIED, OK])
    positive, wrong_ca, wrong_host, after = [c.kwargs["env"] for c in execute.call_args_list[1:]]
    assert positive == after
    assert {k for k in positive if positive[k] != wrong_ca[k]} == {"PGSSLROOTCERT"}
    assert {k for k in positive if positive[k] != wrong_host[k]} == {"PGHOST"}
    assert result.observed == "deny"


def test_wrong_scope_method_digest_version_and_sink_fail_closed(setup):
    probe, evidence = setup
    with pytest.raises(ValueError, match="scope"):
        probe(replace(CTX, app="another"), spec("ENC-01.1"))
    with pytest.raises(ValueError, match="scope"):
        probe(CTX, replace(spec("ENC-01.1"), method_version="2"))
    with patch(
        "bravo_controls.postgres_tls._execute", return_value=(0, b"psql (PostgreSQL) 17.6", b"")
    ):
        with pytest.raises(ValueError, match="version"):
            probe(CTX, spec("ENC-01.1"))
    probe.sink = lambda _: (_ for _ in ()).throw(OSError("sink unavailable"))
    with pytest.raises(OSError, match="sink"):
        invoke(probe, "ENC-01.1", [OK])
    probe.executable.write_bytes(b"changed binary")
    with pytest.raises(ValueError, match="digest"):
        probe(CTX, spec("ENC-01.1"))
    assert evidence == []


@pytest.mark.parametrize(
    "changes",
    [
        {"address": "localhost"},
        {"address": "192.0.2.1,192.0.2.2"},
        {"port": True},
        {"hostname": "pooler.test,elsewhere.test"},
        {"database": "postgresql://user@host/db"},
        {"username": "user\n"},
        {"ca_pem": b"invalid CA"},
        {"ca_pem": b"PRIVATE KEY"},
        {"wrong_hostname": "pooler.test"},
        {"registration_sha256": "missing"},
    ],
)
def test_registration_rejects_ambiguous_or_unbounded_inputs(setup, changes):
    with pytest.raises(ValueError):
        replace(setup[0].target, **changes)


def test_output_bound_and_deadline_are_real(tmp_path):
    with pytest.raises(ValueError, match="budget"):
        _execute(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*100000)"], env={}, cwd=tmp_path
        )
    with pytest.raises(TimeoutError):
        _execute(
            [sys.executable, "-c", "import time;time.sleep(5)"], env={}, cwd=tmp_path, timeout=0.1
        )
    assert _execute([sys.executable, "-c", "print(1)"], env={}, cwd=tmp_path) == (0, b"1\n", b"")


def test_runner_keeps_unimplemented_tls_hops_unknown(setup):
    root = Path(__file__).resolve().parents[3]
    catalog = Catalog(
        json.loads((root / "docs/controls/profiles.json").read_text()),
        json.loads((root / "docs/controls/control-result.schema.json").read_text()),
    )
    probe, _ = setup
    with patch(
        "bravo_controls.postgres_tls._execute",
        side_effect=[(0, b"psql (PostgreSQL) 18.6\n", b""), OK],
    ):
        run = Runner(catalog, {"ENC-01.1": probe}, clock=lambda: NOW).run(
            profile_id="full", profile_version="1", app="demo-chat", environment=ENV
        )
    rows = {x["test_id"]: x for x in run["results"]}
    assert rows["ENC-01.1"]["verdict"] == "PASS"
    assert rows["ENC-01.4"]["verdict"] == rows["ENC-01.5"]["verdict"] == "UNKNOWN"
