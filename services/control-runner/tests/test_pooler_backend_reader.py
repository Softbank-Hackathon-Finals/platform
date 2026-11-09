from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from bravo_controls.pooler_backend_tls import (
    BACKEND_SQL,
    SPEC,
    BackendTarget,
    PoolerBackendTLSProbe,
    PostgreSQLPoolerBackendReader,
)
from bravo_controls.runner import Environment, RunContext

NOW = datetime(2026, 11, 6, tzinfo=UTC)
ENV = Environment("source-fixture", "source", "local:fixture")
TARGET = BackendTarget(
    "chat",
    ENV,
    "pooler",
    "pooler-pod",
    "database",
    "chat",
    "192.0.2.10",
    "192.0.2.20",
    "sha256:" + "a" * 64,
)
CONTEXT = RunContext("backend-reader-fixture", "chat", ENV)
CANARY = "private-credential-diagnostic-must-not-escape"


class Connection:
    def __init__(self, database, user):
        self.closed, self.autocommit = False, True
        self.commands = []
        self.parameters = {
            "sslmode": "verify-full",
            "sslrootcert": "/fixture/ca.crt",
            "host": "pooler.example.invalid",
            "hostaddr": TARGET.pooler_address,
        }
        self.info = SimpleNamespace(
            transaction_status=TransactionStatus.IDLE,
            dbname=database,
            user=user,
            port=6432,
            get_parameters=lambda: self.parameters,
        )
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.rows = (
            [("server_tls_sslmode", "verify-full", "verify-full", "yes")]
            if database == "pgbouncer"
            else [
                (
                    "chat",
                    TARGET.pooler_address,
                    TARGET.database_address,
                    True,
                    "TLSv1.3",
                    42,
                    NOW,
                    "chat_app",
                    "chat_app",
                    "on",
                )
            ]
        )
        self.error = None

    def cursor(self):
        return nullcontext(self)

    def transaction(self):
        return nullcontext()

    def execute(self, statement):
        self.commands.append(statement)
        if self.error:
            raise self.error

    def fetchmany(self, size):
        return self.rows[:size]

    def close(self):
        self.closed = True


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(psycopg, "Connection", Connection)
    stats, app = Connection("pgbouncer", "stats_reader"), Connection("chat", "chat_app")
    opened, registrations = [], []

    def connect():
        assert stats.commands == ["SHOW CONFIG"]
        opened.append(True)
        return app

    def registered(*_):
        registrations.append(True)
        return TARGET.binding()

    value = PostgreSQLPoolerBackendReader(
        stats,
        open_application_connection=connect,
        application_user="chat_app",
        stats_user="stats_reader",
        pooler_hostname="pooler.example.invalid",
        pooler_port=6432,
        read_registration=registered,
        clock=lambda: NOW,
    )
    return value, stats, app, opened, registrations


def invoke(reader):
    evidence = []
    probe = PoolerBackendTLSProbe(
        TARGET, collector=reader, evidence_sink=evidence.append, clock=lambda: NOW
    )
    return probe(CONTEXT, SPEC), evidence


def test_fixed_reads_keep_application_transaction_and_check_registration_twice(reader):
    value, stats, app, opened, registrations = reader
    result, _ = invoke(value)
    assert result.observed == "enabled"
    assert len(opened) == 1 and len(registrations) == 2
    assert stats.commands == ["SHOW CONFIG", "SHOW CONFIG"]
    assert app.commands == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = '3s'",
        "SET LOCAL lock_timeout = '1s'",
        "SET LOCAL TIME ZONE 'UTC'",
        BACKEND_SQL,
    ]
    assert app.closed and not stats.closed
    assert "host(pg_catalog.inet_client_addr())" in BACKEND_SQL


@pytest.mark.parametrize(
    "kind",
    [
        "tls-mode",
        "plain",
        "hostname",
        "address",
        "port",
        "database",
        "user",
        "busy",
        "closed",
        "no-ca",
    ],
)
@pytest.mark.parametrize("which", ["stats", "app"])
def test_wrong_reader_is_not_reused_or_reset(reader, kind, which):
    value, stats, app, _, _ = reader
    c = stats if which == "stats" else app
    if kind == "tls-mode":
        c.parameters["sslmode"] = "require"
    elif kind == "plain":
        c.pgconn.ssl_in_use = False
    elif kind == "hostname":
        c.parameters["host"] = "other.example.invalid"
    elif kind == "address":
        c.parameters["hostaddr"] = "192.0.2.99"
    elif kind == "port":
        c.info.port = 5432
    elif kind == "database":
        c.info.dbname = "another"
    elif kind == "user":
        c.info.user = "another"
    elif kind == "busy":
        c.info.transaction_status = TransactionStatus.INTRANS
    elif kind == "closed":
        c.closed = True
    elif kind == "no-ca":
        c.parameters.pop("sslrootcert")
    result, _ = invoke(value)
    assert result.observed is None
    assert not any("RECONNECT" in cmd or "PAUSE" in cmd for cmd in stats.commands)
    if which == "app":
        assert app.closed


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [("other", "value")],
        [("server_tls_sslmode", "verify-full")] * 2,
        [("server_tls_sslmode", "unknown")],
        [("key", "value")] * 129,
    ],
)
def test_ambiguous_config_blocks_application_connection(reader, rows):
    value, stats, _, opened, _ = reader
    stats.rows = rows
    assert invoke(value)[0].observed is None
    assert not opened


@pytest.mark.parametrize(
    "kind", ["empty", "duplicate", "current-user", "session-user", "not-read-only", "old-backend"]
)
def test_application_query_is_current_authenticated_backend(reader, kind):
    value, _, app, _, _ = reader
    row = list(app.rows[0])
    if kind == "empty":
        app.rows = []
    elif kind == "duplicate":
        app.rows *= 2
    else:
        index, new = {
            "current-user": (7, "other"),
            "session-user": (8, "other"),
            "not-read-only": (9, "off"),
            "old-backend": (6, NOW - timedelta(seconds=1)),
        }[kind]
        row[index] = new
        app.rows = [tuple(row)]
    assert invoke(value)[0].observed is None
    assert app.closed


@pytest.mark.parametrize(
    "error,reason",
    [
        (psycopg.OperationalError, "TOOL_ERROR"),
        (psycopg.errors.InsufficientPrivilege, "OBSERVATION_DENIED"),
        (psycopg.errors.QueryCanceled, "TIMEOUT"),
    ],
)
def test_sql_errors_are_sanitized_and_connection_closed(reader, error, reason):
    value, _, app, _, _ = reader
    app.error = error(CANARY)
    result, evidence = invoke(value)
    assert result.observed is None and result.unknown_reason == reason
    assert CANARY not in repr(evidence)
    assert app.closed


def test_changed_registration_or_deadline_cannot_open_app(reader):
    value, _, _, opened, _ = reader
    value.read_registration = lambda *_: {}
    assert invoke(value)[0].observed is None and not opened
    with pytest.raises(TimeoutError):
        value(CONTEXT, TARGET, NOW)


def test_registration_change_after_query_cannot_publish_success(reader):
    value, _, app, _, _ = reader
    bindings = iter([TARGET.binding(), {}])
    value.read_registration = lambda *_: next(bindings)
    result, _ = invoke(value)
    assert result.observed is None and app.closed


def test_cleanup_error_does_not_publish_success_or_credentials(reader):
    value, _, app, _, _ = reader

    def fail_close():
        raise psycopg.OperationalError(CANARY)

    app.close = fail_close
    result, evidence = invoke(value)
    assert result.observed is None and result.unknown_reason == "TOOL_ERROR"
    assert CANARY not in repr(evidence)
