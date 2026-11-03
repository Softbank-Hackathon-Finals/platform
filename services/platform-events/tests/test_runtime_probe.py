"""Probe semantics on a private temporary DB; never connects to remote resources."""

import json
import secrets
import shutil
import socket
import subprocess
from unittest.mock import Mock, patch

import psycopg
import pytest
from postgres_fixture import TemporaryPostgres
from psycopg import sql
from psycopg.conninfo import make_conninfo

from bravo_platform_events import runtime_probe as probe
from bravo_platform_events.store import EventError


@pytest.fixture
def tls_database():
    """One isolated loopback server, generated CA and one ephemeral SCRAM login."""
    cluster = TemporaryPostgres()
    try:
        database = cluster.database()
        password = secrets.token_urlsafe(32)
        with cluster.connect() as c:
            c.execute(sql.SQL("ALTER ROLE fixture_runtime PASSWORD {}").format(sql.Literal(password)))
        key, cert = cluster.root / "server.key", cluster.root / "server.crt"
        openssl = shutil.which("openssl")
        assert openssl is not None, "OpenSSL required for local verified-TLS contract test"
        config = cluster.root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
        )
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-config", str(config), "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True,
        )
        key.chmod(0o600)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (cluster.data / "postgresql.conf").open("a") as output:
            output.write(
                f"\nlisten_addresses='127.0.0.1'\nport={port}\nssl=on\n"
                f"ssl_cert_file='{cert}'\nssl_key_file='{key}'\n"
            )
        hba = cluster.data / "pg_hba.conf"
        hba.write_text(
            "hostssl all fixture_runtime 127.0.0.1/32 scram-sha-256\n" + hba.read_text()
        )
        cluster.restart()
        yield make_conninfo(
            host="localhost", hostaddr="127.0.0.1", port=port, dbname=database,
            user="fixture_runtime", password=password, sslmode="verify-full", sslrootcert=str(cert),
        )
    finally:
        cluster.close()


def test_actual_verified_tls_cli_and_separate_process_resume(tls_database, tmp_path):
    import os
    import sys

    checkpoint = tmp_path / "checkpoint.json"
    environment = {**os.environ, "BRAVO_PLATFORM_DATABASE_URL": tls_database}
    outputs = []
    for phase in ("exercise", "verify"):
        process = subprocess.run(
            [sys.executable, "-m", "bravo_platform_events.runtime_probe", phase,
             "--checkpoint", str(checkpoint)], env=environment, capture_output=True, text=True,
        )
        assert process.returncode == 0, process.stdout
        result = json.loads(process.stdout)
        assert result["status"] == "PASS"
        assert result["checks"]["tls_verify_full"]
        assert result["checks"]["destructive_table_privileges_absent"]
        assert result["database_restart_performed"] is False
        assert result["backup_restore_verified"] is False
        assert result["http_login_or_deployment_verified"] is False
        assert not process.stderr
        outputs.append(result)
    assert outputs[0]["checkpoint_sha256"] == outputs[1]["checkpoint_sha256"]
    assert "password" not in checkpoint.read_text()
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    # Matching TCP destination with the wrong certificate hostname must fail.
    wrong = make_conninfo(tls_database, host="wrong.example.invalid")
    with pytest.raises(psycopg.OperationalError):
        probe.connection_checks(wrong)


def test_exercise_reconnect_and_local_crash_recovery(pg_backend):
    value = probe.context()
    checks = probe.exercise(pg_backend.reopen, value)
    assert checks["contiguous_event_count"] == 4
    assert checks["failed_write_rollback"]
    assert checks["concurrent_command_retry"]
    assert all(probe.permission_checks(pg_backend.conninfo, value["plan_id"]).values())
    assert probe.retained(pg_backend.reopen(), value)["original_receipt_retry"]
    pg_backend.cluster.restart()
    assert probe.retained(pg_backend.reopen(), value)["retained_state_events_receipt"]


def test_verify_does_not_recreate_missing_state(pg_backend):
    value = probe.context()
    with pytest.raises(EventError, match="plan_not_found"):
        probe.retained(pg_backend.store, value)
    assert pg_backend.store.tenant_for(value["plan_id"]) is None


def test_existing_probe_cannot_be_overwritten(pg_backend):
    value = probe.context()
    probe.exercise(pg_backend.reopen, value)
    before = pg_backend.store.snapshot(value["plan_id"])
    with pytest.raises(probe.ProbeFailure, match="probe_plan_already_exists"):
        probe.exercise(pg_backend.reopen, value)
    assert pg_backend.store.snapshot(value["plan_id"]) == before


def test_valid_but_changed_event_content_is_not_recovery_success(pg_backend):
    value = probe.context()
    probe.exercise(pg_backend.reopen, value)
    events = pg_backend.store.read_after(value["plan_id"], None)
    changed = events[2]
    changed["data"]["status"] = "failed"
    with pg_backend.admin() as c:
        c.execute(
            "UPDATE events SET event_json=%s WHERE plan_id=%s AND sequence=3",
            (json.dumps(changed), value["plan_id"]),
        )
    with pytest.raises(probe.ProbeFailure, match="retained_event_content_mismatch"):
        probe.retained(pg_backend.reopen(), value)


def test_unexpected_update_privilege_is_rolled_back_and_fails(pg_backend):
    value = probe.context()
    probe.exercise(pg_backend.reopen, value)
    with pg_backend.admin() as c:
        c.execute("GRANT UPDATE (tenant_id) ON plans TO bravo_platform_runtime")
    with pytest.raises(probe.ProbeFailure, match="runtime_unexpected_write_privilege"):
        probe.permission_checks(pg_backend.conninfo, value["plan_id"])
    assert pg_backend.store.tenant_for(value["plan_id"]) == "probe-tenant"


def test_missing_table_is_not_counted_as_permission_denial(pg_backend):
    with pg_backend.admin() as c:
        c.execute("ALTER TABLE plans RENAME TO hidden_plans")
    with pytest.raises(psycopg.errors.UndefinedTable):
        probe.permission_checks(pg_backend.conninfo, probe.context()["plan_id"])


@pytest.mark.parametrize("dsn", ["", "host=localhost", "sslmode=require", "sslmode=disable"])
def test_weak_or_implicit_tls_fails_before_connection(dsn):
    with patch.object(probe.psycopg, "connect") as connect:
        with pytest.raises(probe.ProbeFailure, match="verified_tls_connection_required"):
            probe.connection_checks(dsn)
        connect.assert_not_called()


def test_actual_session_must_match_tls_and_runtime_boundaries():
    dsn = "host=db.example.invalid dbname=fixture user=runtime sslmode=verify-full sslrootcert=/fixture/ca"
    connection = Mock()
    connection.info.server_version = 180004

    def run(identity=("runtime", "fixture"), tls=(True,), role=(False,) * 5,
            membership=(True, False, False, False, False),
            ownership=(6, True), destructive=(False,) * 3,
            groups=(("bravo_platform_runtime",),)):
        def execute(query, parameters=()):
            result = Mock()
            result.fetchall.return_value = list(groups)
            result.fetchone.return_value = (
                identity if query == "SELECT current_user, current_database()" else
                tls if "pg_stat_ssl" in query else
                role if "SELECT rolsuper" in query else
                ownership if "pg_catalog.pg_tables" in query else
                destructive if "has_table_privilege" in query else membership
            )
            return result
        connection.execute.side_effect = execute
        manager = Mock()
        manager.__enter__ = Mock(return_value=connection)
        manager.__exit__ = Mock(return_value=False)
        with patch.object(probe.psycopg, "connect", return_value=manager):
            return probe.connection_checks(dsn)

    assert run()["tls_verify_full"] is True
    for changed in [dict(identity=("other", "fixture")), dict(tls=(False,)),
                    dict(role=(True, False, False, False, False)),
                    dict(membership=(True, True, False, False, False)),
                    dict(membership=(True, False, True, False, False)),
                    dict(ownership=(6, False)), dict(destructive=(True, False, False)),
                    dict(groups=(("bravo_platform_runtime",), ("unrelated_admin",)))]:
        with pytest.raises(probe.ProbeFailure):
            run(**changed)


def test_checkpoint_permissions_shape_and_symlink(tmp_path):
    path = tmp_path / "checkpoint.json"
    value = probe.context()
    path.write_text(json.dumps({"format_version": 1, "context": value}))
    path.chmod(0o600)
    assert probe.read_checkpoint(path) == value
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(probe.ProbeFailure, match="checkpoint_must_be_private"):
        probe.read_checkpoint(link)
    path.chmod(0o644)
    with pytest.raises(probe.ProbeFailure, match="checkpoint_must_be_private"):
        probe.read_checkpoint(path)
    path.chmod(0o600)
    path.write_text(json.dumps({"format_version": 1, "context": {**value, "dsn": "fixture"}}))
    with pytest.raises(probe.ProbeFailure, match="invalid_probe_checkpoint"):
        probe.read_checkpoint(path)


def test_cli_failure_does_not_print_connection_or_driver_values(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BRAVO_PLATFORM_DATABASE_URL", "private connection value")
    monkeypatch.setattr("sys.argv", ["probe", "exercise", "--checkpoint", str(tmp_path / "state")])
    monkeypatch.setattr(probe, "connection_checks", Mock(side_effect=RuntimeError("private marker")))
    assert probe.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "FAIL", "error_code": "probe_failed"}
    assert not output.err
    assert not (tmp_path / "state").exists()
