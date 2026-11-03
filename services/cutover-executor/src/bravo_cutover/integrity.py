"""T13 v1: PostgreSQL schema, PK and complete-row fingerprints; no row values in receipts."""

import base64
import hashlib
import json
import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from uuid import UUID

from psycopg import sql
from psycopg.types.json import set_json_loads

from bravo_cutover.remote import Unconfirmed

HASH_RULE = "v1:pg-types-collation;pk-order;typed-json;utf8;sha256-length-prefix"


def canonical(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int | Decimal):
        number = Decimal(value)
        if number == 0 or not number.is_finite():
            return ["number", "0" if number == 0 else str(number)]
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while digits[-1] == 0:
            digits.pop()
            exponent += 1
        return ["number", ("-" if sign else "") + "".join(map(str, digits)) + f"e{exponent}"]
    if isinstance(value, float):
        return ["float", value.hex() if math.isfinite(value) else str(value)]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes | bytearray | memoryview):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return ["timestamptz", value.astimezone(UTC).isoformat(timespec="microseconds")]
        return ["timestamp", value.isoformat(timespec="microseconds")]
    if isinstance(value, date | time | UUID):
        return [type(value).__name__, value.isoformat() if hasattr(value, "isoformat") else str(value)]
    if isinstance(value, list | tuple):
        return ["array", [canonical(v) for v in value]]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return ["object", [[k, canonical(value[k])] for k in sorted(value)]]
    raise Unconfirmed(f"unsupported fingerprint type: {type(value).__name__}")


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _append(digest, value) -> None:
    encoded = _encode(value)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _collation(raw, actual, database, database_actual):
    """Compare semantics, not cluster-local OIDs/names (PG16-18 catalog names)."""
    if raw is None:
        return None
    provider = raw["collprovider"]
    if provider == "d":
        provider = database["datlocprovider"]
        collate, ctype = database["datcollate"], database["datctype"]
        locale = database.get("datlocale", database.get("daticulocale"))
        rules, version, actual = database.get("daticurules"), database["datcollversion"], database_actual
    else:
        collate, ctype = raw["collcollate"], raw["collctype"]
        locale = raw.get("colllocale", raw.get("colliculocale"))
        rules, version = raw.get("collicurules"), raw["collversion"]
    if provider not in ("b", "c", "i") or version != actual:
        raise Unconfirmed("unsupported or stale collation provider/version")
    return [provider, raw["collisdeterministic"], database["encoding"],
            collate, ctype, locale, rules, version, actual]


def fingerprint(conn, table: str) -> dict:
    """Call on a dedicated connection; takes a repeatable-read, read-only snapshot."""
    parts = table.split(".")
    if len(parts) != 2 or not all(parts):
        raise Unconfirmed("comparison requires a schema-qualified table")
    set_json_loads(partial(json.loads, parse_float=Decimal), context=conn)
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        oid_row = conn.execute("SELECT to_regclass(%s)::oid", (table,)).fetchone()
        if not oid_row or oid_row[0] is None:
            raise Unconfirmed(f"comparison table missing: {table}")
        oid = oid_row[0]
        database, database_actual = conn.execute("""SELECT to_jsonb(d),
            pg_database_collation_actual_version(d.oid) FROM pg_database d
            WHERE datname=current_database()""").fetchone()
        columns = conn.execute("""SELECT a.attname, format_type(a.atttypid,a.atttypmod),
            a.attnotnull, a.attidentity, a.attgenerated, pg_get_expr(d.adbin,d.adrelid),
            to_jsonb(c), pg_collation_actual_version(c.oid)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            LEFT JOIN pg_collation c ON c.oid=a.attcollation
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall()
        columns = [(*c[:-2], _collation(c[-2], c[-1], database, database_actual)) for c in columns]
        constraints = conn.execute("""SELECT contype, pg_get_constraintdef(oid, false)
            FROM pg_constraint WHERE conrelid=%s ORDER BY contype, pg_get_constraintdef(oid, false)""", (oid,)).fetchall()
        keys = conn.execute("""SELECT a.attname, a.attcollation <> 0 FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, pos)
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
            WHERE c.conrelid=%s AND c.contype='p' ORDER BY k.pos""", (oid,)).fetchall()
        pk = [row[0] for row in keys]
        if not columns or not pk:
            raise Unconfirmed(f"comparison requires a primary key: {table}")
        schema_hash = hashlib.sha256(_encode([columns, sorted(constraints), pk])).hexdigest()
        names = [c[0] for c in columns]
        pk_indices = [names.index(k) for k in pk]
        # Exact PostgreSQL types are included in schema_hash; values have explicit
        # NULL/binary/time/numeric tags and records have unambiguous length framing.
        pk_digest, content_digest, count = hashlib.sha256(), hashlib.sha256(), 0
        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
            sql.SQL(",").join(map(sql.Identifier, names)), sql.Identifier(*parts),
            sql.SQL(",").join(sql.SQL('{} COLLATE "C"').format(sql.Identifier(k)) if collatable
                              else sql.Identifier(k) for k, collatable in keys))
        with conn.cursor(name="integrity_rows") as cursor:
            cursor.execute(query)
            for row in cursor:
                _append(pk_digest, [canonical(row[i]) for i in pk_indices])
                _append(content_digest, [canonical(v) for v in row])
                count += 1
        return {"rows": count, "schema_hash": schema_hash,
                "pk_hash": pk_digest.hexdigest(), "content_hash": content_digest.hexdigest()}


class IntegrityComparer:
    hash_rule = HASH_RULE

    def compare(self, src, dst, tables: list[str]) -> dict:
        results = []
        for table in tables:
            source, target = src.fingerprint(table), dst.fingerprint(table)
            results.append({"name": table, "rows_src": source["rows"], "rows_dst": target["rows"],
                            "source": source, "target": target, "match": source == target})
        return {"hash_rule": self.hash_rule, "tables": results,
                "verdict": "MATCH" if results and all(t["match"] for t in results) else "MISMATCH"}
