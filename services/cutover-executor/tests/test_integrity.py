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
def pair(postgres_dsn, request):
    names = ["integrity_" + uuid4().hex for _ in range(2)]
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        try:
            options = getattr(request, "param", ["", ""])
            for name, option in zip(names, options, strict=True):
                admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(name)) + sql.SQL(option))
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


def test_identical_rows_with_different_collation_semantics_mismatch(pair):
    seed(pair)
    with psycopg.connect(pair[0], autocommit=True) as conn:
        conn.execute('ALTER TABLE sample ALTER COLUMN content TYPE text COLLATE "C"')
        assert conn.execute("SELECT count(*) FROM sample WHERE content='null'").fetchone()[0] == 0
    with psycopg.connect(pair[1], autocommit=True) as conn:
        conn.execute("CREATE COLLATION foldcase (provider=icu, locale='und-u-ks-level2', deterministic=false)")
        conn.execute('ALTER TABLE sample ALTER COLUMN content TYPE text COLLATE foldcase')
        assert conn.execute("SELECT count(*) FROM sample WHERE content='null'").fetchone()[0] == 1
    result = compare(pair)
    assert result["verdict"] == "MISMATCH"
    source, target = (result["tables"][0][side] for side in ("source", "target"))
    assert source["content_hash"] == target["content_hash"]
    assert source["schema_hash"] != target["schema_hash"]


def test_equivalent_collations_do_not_depend_on_name_or_oid(pair):
    seed(pair)
    for i, dsn in enumerate(pair):
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE COLLATION {} FROM "C"').format(sql.Identifier(f"alias_{i}")))
            conn.execute(sql.SQL('ALTER TABLE sample ALTER COLUMN content TYPE text COLLATE {}')
                         .format(sql.Identifier(f"alias_{i}")))
    assert compare(pair)["verdict"] == "MATCH"


def test_stale_provider_version_is_unconfirmed(pair):
    seed(pair)
    with psycopg.connect(pair[1], autocommit=True) as conn:
        conn.execute("CREATE COLLATION stale (provider=icu, locale='und')")
        conn.execute('ALTER TABLE sample ALTER COLUMN content TYPE text COLLATE stale')
        conn.execute("UPDATE pg_collation SET collversion='old-provider-version' WHERE collname='stale'")
    with pytest.raises(Unconfirmed, match="stale collation"):
        compare(pair)


@pytest.mark.parametrize("pair", [[
    "TEMPLATE template0 LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C'",
    "TEMPLATE template0 LOCALE_PROVIDER icu ICU_LOCALE 'und-u-ks-level2'",
]], indirect=True)
def test_default_collation_resolves_actual_database_locale(pair):
    seed(pair)
    result = compare(pair)
    assert result["verdict"] == "MISMATCH"
    source, target = (result["tables"][0][side] for side in ("source", "target"))
    assert source["content_hash"] == target["content_hash"]
    assert source["schema_hash"] != target["schema_hash"]
