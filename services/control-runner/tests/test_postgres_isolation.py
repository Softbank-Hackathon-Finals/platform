import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import psycopg
import pytest

from bravo_controls.aws_probes import _digest
from bravo_controls.postgres_isolation import (
    AuditWindow,
    DatabasePair,
    PostgreSQLIsolationReader,
    permission_receipt,
)
from bravo_controls.runner import Environment, RunContext

ENV = Environment("source", "source", "fixture/source")
CTX = RunContext("db-isolation", "chat", ENV)
NOW = datetime.now(UTC)
NONCE = "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12
APPNAME = "t14-iso-" + NONCE
ENDPOINT = ("127.0.0.1", 44001)


@pytest.fixture
def pair(tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"fixture CA")
    return DatabasePair(
        "chat",
        "other",
        ENV,
        "cluster-1",
        "localhost",
        "127.0.0.1",
        5432,
        ca,
        _digest(ca.read_bytes()),
        "chat_db",
        "other_db",
        "chat_user",
        "other_user",
        100,
        101,
        200,
        201,
    )


def record(pair, **updates):
    return json.dumps(
        {
            "application_name": APPNAME,
            "remote_host": ENDPOINT[0],
            "remote_port": ENDPOINT[1],
            "timestamp": NOW.isoformat(),
            "error_severity": "FATAL",
            "state_code": "42501",
            "user": pair.other_user,
            "dbname": pair.owner_database,
            "message": "SECRET-NOT-TO-PERSIST",
            **updates,
        }
    ).encode()


def window(pair, *lines):
    return AuditWindow(pair.cluster_uid, NOW, NOW, True, lines or (record(pair),), ENDPOINT)


def test_only_structured_fatal_permission_is_denial(pair):
    receipt = permission_receipt(window(pair), pair, APPNAME, NOW, NOW, ENDPOINT)
    assert receipt.startswith("sha256:") and "SECRET" not in receipt


@pytest.mark.parametrize(
    "changes",
    [
        {"application_name": "replay"},
        {"state_code": "28P01"},
        {"state_code": "08001"},
        {"error_severity": "LOG"},
        {"user": "foreign_user"},
        {"dbname": "foreign_db"},
        {"timestamp": (NOW - timedelta(seconds=10)).isoformat()},
        {"timestamp": (NOW + timedelta(seconds=10)).isoformat()},
    ],
)
def test_unrelated_or_auth_transport_failure_not_denial(pair, changes):
    assert (
        permission_receipt(window(pair, record(pair, **changes)), pair, APPNAME, NOW, NOW, ENDPOINT)
        is None
    )


def test_duplicate_or_conflicting_events_not_denial(pair):
    for lines in ((record(pair), record(pair)), (record(pair), record(pair, state_code="28P01"))):
        assert permission_receipt(window(pair, *lines), pair, APPNAME, NOW, NOW, ENDPOINT) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"cluster_uid": "foreign"},
        {"started_at": NOW - timedelta(seconds=1)},
        {"lines": (b"{",)},
        {"lines": (b"[]",)},
        {"lines": (b"x" * 65537,)},
    ],
)
def test_bad_audit_window_rejected(pair, changes):
    with pytest.raises(ValueError):
        permission_receipt(replace(window(pair), **changes), pair, APPNAME, NOW, NOW, ENDPOINT)


def connection(pair, *, other=False, cross=False):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    user = pair.other_user if other else pair.owner_user
    db = pair.owner_database if cross or not other else pair.other_database
    role_oid = pair.other_role_oid if other else pair.owner_role_oid
    db_oid = pair.owner_database_oid if cross or not other else pair.other_database_oid
    conn.execute.return_value.fetchone.return_value = (db, user, user, db_oid, role_oid, True)
    return conn


@pytest.fixture
def reader(pair):
    value = PostgreSQLIsolationReader(
        pair,
        passwords=lambda _: "PRIVATE-PASSWORD",
        read_registration=lambda _: pair.binding,
        read_audit=lambda *_: window(pair),
        clock=lambda: NOW,
    )

    value._connect_cross = Mock(return_value=(None, ENDPOINT))
    return value


def calls(pair, cross):
    return [
        connection(pair),
        connection(pair, other=True),
        connection(pair),
        connection(pair, other=True),
    ]


def test_connection_failure_needs_correlated_audit(reader, pair):
    with patch(
        "psycopg.connect", side_effect=calls(pair, psycopg.OperationalError("PRIVATE-ERROR"))
    ) as connect:
        result = reader(CTX, NONCE)
    assert result.outcome == "deny" and result.positive
    assert "PRIVATE" not in repr(result)
    for call in connect.call_args_list:
        assert call.kwargs["sslmode"] == "verify-full"
        assert call.kwargs["require_auth"] == "scram-sha-256"
        assert call.kwargs["channel_binding"] == "require"
        assert call.kwargs["connect_timeout"] == 3
        assert "read_only=on" in call.kwargs["options"]
    assert reader._connect_cross.call_args.args[:2] == (pair.other_user, pair.owner_database)


def test_cross_connection_allowed_is_failure_observation(reader, pair):
    reader.audit = Mock(side_effect=AssertionError("no rejection audit for success"))
    reader._connect_cross.return_value = (connection(pair, other=True, cross=True), ENDPOINT)
    with patch(
        "psycopg.connect", side_effect=calls(pair, connection(pair, other=True, cross=True))
    ):
        result = reader(CTX, NONCE)
    assert result.outcome == "allow" and result.positive


def test_failure_without_audit_is_unknown(reader, pair):
    reader.audit = lambda *_: replace(window(pair), lines=())
    with patch("psycopg.connect", side_effect=calls(pair, psycopg.OperationalError("no response"))):
        result = reader(CTX, NONCE)
    assert result.outcome is None and result.reason == "UNEXPECTED_RESPONSE"


def test_invalid_other_password_positive_prevents_negative(reader, pair):
    with patch(
        "psycopg.connect", side_effect=[connection(pair), psycopg.OperationalError("bad password")]
    ) as connect:
        result = reader(CTX, NONCE)
    assert result.outcome is None and result.reason == "POSITIVE_CONTROL_FAILED"
    assert connect.call_count == 2


def test_recreated_role_oid_rejected(reader, pair):
    changed = connection(pair)
    changed.execute.return_value.fetchone.return_value = (
        pair.owner_database,
        pair.owner_user,
        pair.owner_user,
        100,
        999,
        True,
    )
    with patch("psycopg.connect", return_value=changed) as connect:
        result = reader(CTX, NONCE)
    assert not result.positive and connect.call_count == 1


def test_postpositive_failure_invalidates_denial(reader, pair):
    attempts = calls(pair, psycopg.OperationalError("denied"))
    attempts[2] = psycopg.OperationalError("down")
    with patch("psycopg.connect", side_effect=attempts):
        result = reader(CTX, NONCE)
    assert result.outcome is None and not result.positive


def test_ca_drift_prevents_password_transmission(reader, pair):
    pair.ca_file.write_bytes(b"changed")
    with patch("psycopg.connect") as connect:
        with pytest.raises(ValueError):
            reader(CTX, NONCE)
    connect.assert_not_called()


def test_live_registration_change_rejected(reader, pair):
    reader.registration = Mock(side_effect=[pair.binding, "changed"])
    with patch("psycopg.connect", side_effect=calls(pair, psycopg.OperationalError("denied"))):
        with pytest.raises(ValueError, match="registration changed"):
            reader(CTX, NONCE)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_user": "other_user"},
        {"other_app": "chat"},
        {"other_database": "chat_db"},
        {"other_role_oid": 200},
        {"owner_role_oid": True},
        {"hostaddr": "0.0.0.0"},
    ],
)
def test_registration_invalid(pair, changes):
    with pytest.raises(ValueError):
        replace(pair, **changes)


def test_real_pg_early_rejection_without_application_name_is_correlated(pair):
    row = record(pair, application_name=None)
    assert permission_receipt(window(pair, row), pair, APPNAME, NOW, NOW, ENDPOINT)


@pytest.mark.parametrize("changes", [{"remote_host": "127.0.0.2"}, {"remote_port": 44002}])
def test_other_connection_not_denial(pair, changes):
    assert (
        permission_receipt(window(pair, record(pair, **changes)), pair, APPNAME, NOW, NOW, ENDPOINT)
        is None
    )


def test_audit_endpoint_mapping_must_not_be_substituted(pair):
    changed = replace(window(pair), client_endpoint=("127.0.0.1", 44002))
    with pytest.raises(ValueError):
        permission_receipt(changed, pair, APPNAME, NOW, NOW, ENDPOINT)


def test_pg_millisecond_timestamp_precision(pair):
    stamp = NOW.replace(microsecond=NOW.microsecond // 1000 * 1000)
    assert permission_receipt(
        window(pair, record(pair, timestamp=stamp.isoformat())), pair, APPNAME, NOW, NOW, ENDPOINT
    )


def test_libpq_failure_retains_endpoint_and_closes(pair):
    from psycopg import pq

    reader = PostgreSQLIsolationReader(
        pair,
        passwords=lambda _: "fixture",
        read_registration=lambda _: pair.binding,
        read_audit=lambda *_: None,
    )
    raw = Mock(socket=9)
    raw.connect_poll.return_value = pq.PollingStatus.FAILED
    duplicate = MagicMock()
    duplicate.__enter__.return_value.getsockname.return_value = ENDPOINT
    with (
        patch("psycopg.pq.PGconn") as factory,
        patch("bravo_controls.postgres_isolation.socket.fromfd", return_value=duplicate),
    ):
        factory.connect_start.return_value = raw
        conn, endpoint = reader._connect_cross(
            pair.other_user, pair.owner_database, "fixture", APPNAME
        )
    assert conn is None and endpoint == ENDPOINT
    raw.finish.assert_called_once()
    duplicate.__exit__.assert_called_once()


def test_libpq_wait_timeout_finishes_connection(pair):
    from psycopg import pq

    reader = PostgreSQLIsolationReader(
        pair,
        passwords=lambda _: "fixture",
        read_registration=lambda _: pair.binding,
        read_audit=lambda *_: None,
    )
    raw = Mock(socket=9)
    raw.connect_poll.return_value = pq.PollingStatus.READING
    duplicate = MagicMock()
    duplicate.__enter__.return_value.getsockname.return_value = ENDPOINT
    with (
        patch("psycopg.pq.PGconn") as factory,
        patch("bravo_controls.postgres_isolation.socket.fromfd", return_value=duplicate),
        patch("bravo_controls.postgres_isolation.select.select", return_value=([], [], [])),
    ):
        factory.connect_start.return_value = raw
        with pytest.raises(TimeoutError):
            reader._connect_cross(pair.other_user, pair.owner_database, "fixture", APPNAME)
    raw.finish.assert_called_once()
