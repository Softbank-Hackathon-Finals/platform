"""Read-only query for the qualified demo-chat DR-01.1 marker path.

The caller binds a dedicated psycopg connection to the registered restored DB,
verifies its Kubernetes UID and backup provenance, and never reuses an app pool.
Only the fixed public.messages schema is supported; no user-selected SQL runs.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True)
class MarkerReadback:
    marker_id: int | None
    marker_sha256: str | None
    row_count: int
    observed_at: datetime


def read_demo_chat_marker(
    connection, *, marker_id: int, idempotency_key: str, clock=lambda: datetime.now(UTC)
) -> MarkerReadback:
    from psycopg import Connection
    from psycopg.pq import TransactionStatus

    if (
        not isinstance(connection, Connection)
        or connection.closed
        or connection.info.transaction_status != TransactionStatus.IDLE
        or connection.info.get_parameters().get("sslmode") != "verify-full"
        or not connection.pgconn.ssl_in_use
    ):
        raise ValueError("idle dedicated certificate-verified PostgreSQL connection required")
    if type(marker_id) is not int or not 1 <= marker_id <= 2**63 - 1:
        raise ValueError("positive bigint marker ID required")
    if not isinstance(idempotency_key, str) or str(UUID(idempotency_key)) != idempotency_key:
        raise ValueError("canonical marker UUID required")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL lock_timeout = '2s'")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            cursor.execute("SELECT count(*) FROM public.messages WHERE id <= %s", (marker_id,))
            count_row = cursor.fetchone()
            if (
                not isinstance(count_row, tuple)
                or len(count_row) != 1
                or type(count_row[0]) is not int
                or count_row[0] < 0
            ):
                raise ValueError("invalid restore count result")
            cursor.execute(
                "SELECT id, CASE WHEN octet_length(to_jsonb(m)::text) <= 1048576 "
                "THEN to_jsonb(m)::text ELSE NULL END FROM public.messages m "
                "WHERE idempotency_key = %s LIMIT 2",
                (idempotency_key,),
            )
            rows = cursor.fetchall()
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError("ambiguous restore marker")
            found_id = found_hash = None
            if rows:
                row = rows[0]
                if (
                    not isinstance(row, tuple)
                    or len(row) != 2
                    or type(row[0]) is not int
                    or not 1 <= row[0] <= 2**63 - 1
                    or not isinstance(row[1], str)
                    or not row[1]
                    or len(row[1].encode()) > 1024 * 1024
                ):
                    raise ValueError("invalid restore marker row")
                found_id = row[0]
                found_hash = "sha256:" + hashlib.sha256(row[1].encode()).hexdigest()
    observed = clock()
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() is None
    ):
        raise ValueError("aware readback clock required")
    return MarkerReadback(found_id, found_hash, count_row[0], observed.astimezone(UTC))
