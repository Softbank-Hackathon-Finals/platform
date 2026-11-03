"""Actual PostgreSQL/TLS schema capture, synthetic context/replication authority."""
import secrets
import subprocess
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from bravo_cutover.db_fingerprint import HASH_RULE
from psycopg import sql

from bravo_gate.contract import GateError
from bravo_gate.readiness import ReadinessPolicy, assess_readiness
from bravo_gate.t13 import DatabaseRegistration, assemble_snapshot, collect_schema

IMAGE = "public.ecr.aws/docker/library/postgres:18.4@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636"


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=90).stdout.strip()


@pytest.fixture(scope="module")
def tls_postgres(tmp_path_factory):
    root = tmp_path_factory.mktemp("t19-tls")
    root.chmod(0o755)
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=t19-test", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout", str(root / "server.key"), "-out", str(root / "server.crt"))
    # Test-only key; the container copies it to mode 0600, postgres owner.
    (root / "server.key").chmod(0o644)
    (root / "server.crt").chmod(0o644)
    password = secrets.token_urlsafe(32)
    cid = None
    try:
        cid = run("docker", "run", "-d", "--rm", "--name", "t19-schema-" + secrets.token_hex(6),
                  "-e", "POSTGRES_PASSWORD=" + password, "-v", str(root) + ":/certs:ro",
                  "-p", "127.0.0.1::5432", "--entrypoint", "sh", IMAGE, "-c",
                  "cp /certs/server.key /tmp/t19-server.key && chown postgres /tmp/t19-server.key && chmod 600 /tmp/t19-server.key && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/certs/server.crt -c ssl_key_file=/tmp/t19-server.key")
        port = int(run("docker", "port", cid, "5432/tcp").split(":")[-1])
        kwargs = dict(host="127.0.0.1", port=port, user="postgres", password=password,
                      dbname="postgres", sslmode="verify-full", sslrootcert=str(root / "server.crt"),
                      connect_timeout=2, autocommit=True)
        until = time.monotonic() + 45
        while True:
            try:
                with psycopg.connect(**kwargs):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= until:
                    pytest.fail("temporary TLS PostgreSQL did not start", pytrace=False)
                time.sleep(0.25)
        with psycopg.connect(**kwargs) as conn:
            conn.execute(sql.SQL("CREATE ROLE reader LOGIN PASSWORD {}").format(sql.Literal(password)))
            conn.execute("ALTER ROLE reader SET default_transaction_read_only = on")
        yield kwargs
    finally:
        if cid:
            run("docker", "rm", "-f", cid)


@pytest.fixture
def actual_capture(service, tls_postgres):
    _, state = service
    ctx = state["context"]
    policy = ReadinessPolicy("schema-demo.v1", HASH_RULE, ("public.messages",), (), 0, 30)
    admin = dict(tls_postgres)
    regs = {}
    for side in ("source", "target"):
        name = "schema_" + secrets.token_hex(6)
        with psycopg.connect(**admin) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with psycopg.connect(**(admin | {"dbname": name})) as conn:
            conn.execute("CREATE TABLE public.messages(id bigint PRIMARY KEY, body text NOT NULL)")
            conn.execute("GRANT USAGE ON SCHEMA public TO reader")
            conn.execute("GRANT SELECT ON public.messages TO reader")
        regs[side] = DatabaseRegistration(getattr(ctx, side + "_environment_id"),
            getattr(ctx, side + "_environment_ref"), "db-ref-"+side, name, "reader")
    def connect(reg):
        return psycopg.connect(**(admin | {"dbname": reg.database_name, "user": reg.role_name}))
    def capture(side, **overrides):
        args = dict(side=side, connect=connect, clock=lambda: datetime.now(UTC))
        args.update(overrides)
        return collect_schema(ctx, policy, regs[side], **args)
    return ctx, policy, regs, connect, capture, admin


def combined_data(f, source, target):
    ctx, policy, regs, *_ = f
    now = datetime.now(UTC)
    # Explicit synthetic replication record: test only the schema integration.
    rep = dict(source_database_ref=regs["source"].database_ref, target_database_ref=regs["target"].database_ref,
               lag_bytes=0, observed_at=now.isoformat(), expires_at=(now+timedelta(seconds=20)).isoformat())
    binding, snapshot = assemble_snapshot(ctx, policy, source, target, snapshot_ref="t13-schema-test", replication=rep, sequences=[])
    return assess_readiness(ctx, binding, snapshot, now=now)


def test_actual_tls_schema_match_then_mismatch_blocks(actual_capture):
    f = actual_capture
    _, _, regs, _, capture, admin = f
    a, b = capture("source"), capture("target")
    assert a.errors == b.errors == ()
    assert combined_data(f, a, b)["decision"] == "PASS"
    with psycopg.connect(**(admin | {"dbname": regs["target"].database_name})) as conn:
        conn.execute("ALTER TABLE public.messages ADD COLUMN extra text")
    result = combined_data(f, a, capture("target"))
    assert result["decision"] == "BLOCK"
    assert any(r["reason"] == "SCHEMA_MISMATCH" for r in result["rows"])


@pytest.mark.parametrize("fault", ["non-tls", "no-verification", "wrong-database", "superuser", "not-autocommit", "busy"])
def test_connection_boundary_cannot_return_schema_pass(actual_capture, fault):
    _, _, _, _, capture, admin = actual_capture
    @contextmanager
    def bad(reg):
        params = admin | {"dbname": reg.database_name, "user": reg.role_name}
        if fault == "non-tls":
            params["sslmode"] = "disable"
        if fault == "no-verification":
            params["sslmode"] = "require"
        if fault == "wrong-database":
            params["dbname"] = "postgres"
        if fault == "superuser":
            params["user"] = "postgres"
        if fault in ("not-autocommit", "busy"):
            params["autocommit"] = False
        with psycopg.connect(**params) as conn:
            if fault == "busy":
                conn.execute("SELECT 1")
            yield conn
    result = capture("source", connect=bad)
    assert result.errors == (("public.messages", "SCHEMA_CAPTURE_UNAVAILABLE"),)
    assert result.document["tables"][0]["schema_hash"] is None


def test_unsupported_schema_is_unknown_and_database_not_mutated(actual_capture):
    _, _, regs, connect, capture, admin = actual_capture
    with psycopg.connect(**(admin | {"dbname": regs["target"].database_name})) as conn:
        conn.execute("ALTER TABLE public.messages ADD COLUMN unsupported money")
    result = capture("target")
    assert result.document["tables"][0]["schema_hash"] is None
    assert result.errors == (("public.messages", "SCHEMA_UNCONFIRMED"),)
    with connect(regs["target"]) as conn:
        assert conn.execute("SELECT count(*) FROM public.messages").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO public.messages(id,body) VALUES (1,'forbidden')")


def test_timeout_clears_partial_success(actual_capture):
    _, _, _, _, capture, _ = actual_capture
    start = datetime.now(UTC)
    times = iter([start, start, start+timedelta(seconds=31)])
    result = capture("source", clock=lambda: next(times))
    assert result.errors == (("public.messages", "SCHEMA_CAPTURE_UNAVAILABLE"),)
    assert result.document["tables"][0]["schema_hash"] is None


def test_wrong_context_or_rule_is_rejected_before_connect(actual_capture):
    ctx, policy, regs, *_ = actual_capture
    def forbidden(_):
        pytest.fail("connection must not be reached")
    with pytest.raises(GateError, match="RESOURCE_MISMATCH"):
        collect_schema(ctx, policy, regs["source"], side="target", connect=forbidden, clock=lambda: datetime.now(UTC))
    with pytest.raises(GateError, match="RULE_MISMATCH"):
        collect_schema(ctx, replace(policy, schema_rule="wrong"), regs["source"], side="source", connect=forbidden, clock=lambda: datetime.now(UTC))


def test_changed_context_cannot_reuse_capture(actual_capture):
    ctx, policy, _, _, capture, _ = actual_capture
    a, b = capture("source"), capture("target")
    with pytest.raises(GateError, match="CAPTURE_BINDING_MISMATCH"):
        assemble_snapshot(replace(ctx, gate_id="another-gate"), policy, a, b, snapshot_ref="x", replication={}, sequences=[])


def test_schema_change_during_collection_is_unknown(actual_capture, monkeypatch):
    import bravo_cutover.db_fingerprint as t13
    _, _, regs, _, capture, admin = actual_capture
    real = t13.schema_fingerprint
    calls = 0
    def changing(conn, table):
        nonlocal calls
        result = real(conn, table)
        calls += 1
        if calls == 1:
            with psycopg.connect(**(admin | {"dbname": regs["source"].database_name})) as writer:
                writer.execute("ALTER TABLE public.messages ADD COLUMN during_capture text")
        return result
    monkeypatch.setattr(t13, "schema_fingerprint", changing)
    result = capture("source")
    assert result.errors == (("public.messages", "SCHEMA_CHANGED_OR_UNCONFIRMED"),)
    assert result.document["tables"][0]["schema_hash"] is None


def test_wrong_tls_hostname_never_yields_a_capture(actual_capture):
    _, _, _, _, capture, admin = actual_capture
    def wrong(reg):
        return psycopg.connect(**(admin | {"dbname":reg.database_name, "user":reg.role_name,
                                           "host":"untrusted.invalid", "hostaddr":"127.0.0.1"}))
    result = capture("source", connect=wrong)
    assert result.errors == (("public.messages", "SCHEMA_CAPTURE_UNAVAILABLE"),)
    assert result.document["tables"][0]["schema_hash"] is None


def test_connection_error_does_not_expose_details(actual_capture):
    _, _, _, _, capture, _ = actual_capture
    def unavailable(_):
        raise RuntimeError("private-connection-detail-canary")
    result = capture("source", connect=unavailable)
    assert "private-connection-detail-canary" not in repr(result)
    assert result.document["tables"][0]["schema_hash"] is None


def test_capture_expiry_is_used_by_gate(actual_capture):
    ctx, policy, regs, _, capture, _ = actual_capture
    old = datetime.now(UTC) - timedelta(seconds=60)
    source = capture("source", clock=lambda: old)
    target = capture("target", clock=lambda: old)
    assert source.errors == target.errors == ()  # schema was read; time is independently checked
    result = combined_data(actual_capture, source, target)
    assert result["decision"] == "BLOCK"
    assert sum(r["reason"] == "STALE_OR_FUTURE" for r in result["rows"]) == 2


@pytest.mark.parametrize("fault", ["unknown-lag", "missing-sequence", "missing-replication"])
def test_matching_actual_schema_cannot_replace_missing_operational_evidence(actual_capture, fault):
    ctx, policy, regs, connect, _, _ = actual_capture
    policy = replace(policy, sequence_columns=(("public.messages", "id"),))
    captures = [collect_schema(ctx, policy, regs[side], side=side, connect=connect,
                              clock=lambda: datetime.now(UTC)) for side in ("source", "target")]
    assert all(not item.errors for item in captures)
    now = datetime.now(UTC)
    rep = dict(source_database_ref=regs["source"].database_ref,
               target_database_ref=regs["target"].database_ref, lag_bytes=None if fault == "unknown-lag" else 0,
               observed_at=now.isoformat(), expires_at=(now + timedelta(seconds=20)).isoformat())
    if fault == "missing-replication":
        rep = {}
    binding, snapshot = assemble_snapshot(ctx, policy, *captures,
        snapshot_ref="missing-operational-evidence", replication=rep, sequences=[])
    if fault == "missing-replication":
        with pytest.raises(GateError, match="READINESS_SHAPE_INVALID"):
            assess_readiness(ctx, binding, snapshot, now=now)
    else:
        result = assess_readiness(ctx, binding, snapshot, now=now)
        assert result["decision"] == "BLOCK"
        assert any(row["reason"] == "MISSING_SEQUENCE" for row in result["rows"])
        if fault == "unknown-lag":
            assert any(row["test_id"] == "replication:lag" and row["reason"] == "UNKNOWN"
                       for row in result["rows"])
