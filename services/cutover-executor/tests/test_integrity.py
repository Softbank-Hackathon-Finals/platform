"""Complete content/PK/schema comparison against two disposable PostgreSQL databases."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from bravo_cutover.integrity import IntegrityComparer, canonical
from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.db import DbControl


@pytest.fixture
def pair(postgres_dsn):
    names = ["integrity_" + uuid4().hex for _ in range(2)]
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        try:
            for name in names:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            dsns = [make_conninfo(**(conninfo_to_dict(postgres_dsn) | {"dbname": name})) for name in names]
            for dsn in dsns:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute("""CREATE TABLE sample (id text PRIMARY KEY, content text, amount numeric,
                        payload jsonb, payload_bytes bytea, created_at timestamptz)""")
            yield dsns
        finally:
            for name in names:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def compare(pair):
    return IntegrityComparer().compare(*(DbControl(dsn) for dsn in pair), ["public.sample"])


def seed(pair):
    values = [("가", None, Decimal("123456789012345678901234567890.123456789")),
              ("a", "NULL", Decimal("1.00"))]
    for i, dsn in enumerate(pair):
        with psycopg.connect(dsn, autocommit=True) as conn:
            for key, text, number in values[::1 if i == 0 else -1]:
                conn.execute("""INSERT INTO sample VALUES (%s,%s,%s,
                    '{"a":123456789012345678901234567890.123456789,"b":[true,null]}',
                    %s,'2026-11-03T12:00:00Z')""", (key, text, number, b"\x00\xff"))


def test_full_rows_match_independent_of_insertion_order(pair):
    seed(pair)
    result = compare(pair)
    assert result["verdict"] == "MATCH"
    assert result["tables"][0]["source"]["rows"] == 2
    assert "123456789" not in str(result)  # receipt contains hashes, never row values


@pytest.mark.parametrize("query", [
    "UPDATE sample SET content='changed' WHERE id='a'",
    "UPDATE sample SET id='different' WHERE id='a'",
    "UPDATE sample SET content='NULL' WHERE id='가'",
    "UPDATE sample SET amount=123456789012345678901234567890.123456788 WHERE id='가'",
    "ALTER TABLE sample ALTER COLUMN content SET DEFAULT 'different'",
    "ALTER TABLE sample ADD CONSTRAINT amount_positive CHECK(amount>0)",
])
def test_equal_row_counts_do_not_hide_content_pk_or_schema_changes(pair, query):
    seed(pair)
    with psycopg.connect(pair[1], autocommit=True) as conn:
        conn.execute(query)
    result = compare(pair)
    assert result["verdict"] == "MISMATCH"
    assert result["tables"][0]["rows_src"] == result["tables"][0]["rows_dst"] == 2


def test_missing_primary_key_is_unconfirmed(pair):
    with psycopg.connect(pair[1], autocommit=True) as conn:
        conn.execute("ALTER TABLE sample DROP CONSTRAINT sample_pkey")
    with pytest.raises(Unconfirmed, match="primary key"):
        compare(pair)


def test_canonical_numbers_and_timestamps_are_exact():
    assert canonical(Decimal("1.000")) == canonical(1)
    assert canonical(Decimal("123456789012345678901234567890.1")) != canonical(Decimal("123456789012345678901234567890.2"))
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert canonical(now) == canonical(now.astimezone(timezone(timedelta(hours=9))))
    assert canonical(None) != canonical("null")
