import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus

from bravo_controls.postgres_restore import read_demo_chat_marker

MARKER_UUID = "34ac167e-e6f7-4521-b6b3-90f49db39710"
NOW = datetime(2026, 11, 3, tzinfo=UTC)


def connection():
    c = MagicMock(spec=Connection)
    c.closed = False
    c.info.transaction_status = TransactionStatus.IDLE
    c.info.get_parameters.return_value = {"sslmode": "verify-full"}
    c.pgconn = MagicMock(ssl_in_use=True)
    cursor = c.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (3,)
    cursor.fetchall.return_value = [(3, '{"id": 3, "body": "private row"}')]
    return c, cursor


def test_fixed_read_only_snapshot_query_and_hash_only():
    c, cur = connection()
    result = read_demo_chat_marker(c, marker_id=3, idempotency_key=MARKER_UUID, clock=lambda: NOW)
    assert result.row_count == 3 and result.marker_id == 3
    assert (
        result.marker_sha256
        == "sha256:" + hashlib.sha256(b'{"id": 3, "body": "private row"}').hexdigest()
    )
    calls = cur.execute.call_args_list
    assert calls[0].args == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",)
    assert calls[-2].args[1] == (3,)
    assert calls[-1].args[1] == (MARKER_UUID,)
    assert "private row" not in repr(result)


@pytest.mark.parametrize("kind", ["closed", "transaction", "no_tls", "weak_tls"])
def test_connection_boundary_rejected(kind):
    c, cur = connection()
    if kind == "closed":
        c.closed = True
    elif kind == "transaction":
        c.info.transaction_status = TransactionStatus.INTRANS
    elif kind == "no_tls":
        c.pgconn.ssl_in_use = False
    else:
        c.info.get_parameters.return_value = {"sslmode": "require"}
    with pytest.raises(ValueError):
        read_demo_chat_marker(c, marker_id=3, idempotency_key=MARKER_UUID)
    cur.execute.assert_not_called()


@pytest.mark.parametrize("rows", [[], [(4, '{"id": 4}')]])
def test_missing_or_different_marker_is_readback_not_implicit_success(rows):
    c, cur = connection()
    cur.fetchall.return_value = rows
    result = read_demo_chat_marker(c, marker_id=3, idempotency_key=MARKER_UUID)
    assert result.marker_id == (4 if rows else None)


@pytest.mark.parametrize(
    "count,rows",
    [
        ((True,), [(3, "{}")]),
        ((-1,), [(3, "{}")]),
        ((3,), [(3, "{}"), (3, "{}")]),
        ((3,), [(True, "{}")]),
        ((3,), [(3, "")]),
        ((3,), [(3, "a" * (1024 * 1024 + 1))]),
    ],
)
def test_incomplete_or_unbounded_readback_rejected(count, rows):
    c, cur = connection()
    cur.fetchone.return_value = count
    cur.fetchall.return_value = rows
    with pytest.raises(ValueError):
        read_demo_chat_marker(c, marker_id=3, idempotency_key=MARKER_UUID)


def test_query_failure_propagates_without_row_disclosure():
    c, cur = connection()
    cur.execute.side_effect = PermissionError("query denied")
    with pytest.raises(PermissionError):
        read_demo_chat_marker(c, marker_id=3, idempotency_key=MARKER_UUID)
