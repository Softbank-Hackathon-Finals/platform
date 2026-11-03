"""T13: 무결성 지문을 DB 안에서 계산한다. 행은 네트워크로 나오지 않고 지문과 카탈로그 정보만 나온다.

판정 모듈이라 LLM을 쓰지 않는다. 확인할 수 없으면 Unconfirmed를 던지고, 비교기는 UNKNOWN으로 남긴다.
규칙과 시험 사례는 docs/contracts/T13-db-fingerprint-v1.md에 있다.
"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql

from bravo_cutover.remote import Unconfirmed

# 값을 글자로 바꿔 비교하므로 numeric 1.5와 1.50, jsonb 1과 1.0은 다른 값이다. 규칙이 바뀌면 v2로 올린다.
HASH_RULE = "db-sql-v1:fixed-session-text;columns-by-name;jsonb-array;utf8;sha256-of-row-sha256;pk-order-collate-c"
STATEMENT_TIMEOUT_MS = 3000

MATCH, MISMATCH, UNKNOWN = "MATCH", "MISMATCH", "UNKNOWN"

# 아래 세션 설정으로 ::text 결과가 하나로 정해지는 타입만 허용한다. 그 밖(money · 배열 · enum · 도메인 등)은 불명이다.
_SUPPORTED_TYPES = [
    "boolean", "smallint", "integer", "bigint", "numeric", "real", "double precision",
    "text", "character varying", "character", "bytea", "uuid", "date",
    "time without time zone", "time with time zone",
    "timestamp without time zone", "timestamp with time zone", "interval", "json", "jsonb",
]

# search_path를 고정해 함수 · 타입 이름이 가로채이지 않게 한다. row_security=off는 정책이 행을 숨기면 오류를 낸다.
_SNAPSHOT = f"""SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS};
SET LOCAL search_path = pg_catalog;
SET LOCAL row_security = off;
SET LOCAL TimeZone = 'UTC';
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL IntervalStyle = 'postgres';
SET LOCAL extra_float_digits = 1;
SET LOCAL bytea_output = 'hex'"""

_CATALOG = """SELECT c.relkind::text, c.relhassubclass,
  (SELECT to_jsonb(d) || jsonb_build_object('actual_version', pg_database_collation_actual_version(d.oid))
     FROM pg_database d WHERE d.datname = current_database()),
  (SELECT jsonb_agg(jsonb_build_object(
            'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod), 'not_null', a.attnotnull,
            'identity', a.attidentity, 'generated', a.attgenerated, 'default', pg_get_expr(df.adbin, df.adrelid),
            'supported', a.atttypid::regtype::text = ANY(%(types)s),
            'collation', to_jsonb(co), 'collation_actual', pg_collation_actual_version(co.oid)) ORDER BY a.attnum)
     FROM pg_attribute a
     LEFT JOIN pg_attrdef df ON df.adrelid = a.attrelid AND df.adnum = a.attnum
     LEFT JOIN pg_collation co ON co.oid = a.attcollation
    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
  (SELECT jsonb_agg(jsonb_build_array(o.contype, pg_get_constraintdef(o.oid, false)))
     FROM pg_constraint o WHERE o.conrelid = c.oid),
  (SELECT jsonb_agg(a.attname ORDER BY k.pos)
     FROM pg_constraint o CROSS JOIN LATERAL unnest(o.conkey) WITH ORDINALITY AS k(attnum, pos)
     JOIN pg_attribute a ON a.attrelid = o.conrelid AND a.attnum = k.attnum
    WHERE o.conrelid = c.oid AND o.contype = 'p')
FROM pg_class c WHERE c.oid = to_regclass(%(table)s)"""


def _sha256(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _collation(raw, actual, database) -> tuple[list | None, str | None]:
    """이름 · OID가 아니라 의미를 돌려준다. 기본 정렬은 DB 설정으로 푼다(#134 98a9fec와 같은 검사)."""
    if raw is None:
        return None, None
    provider = raw["collprovider"]
    if provider == "d":
        provider = database["datlocprovider"]
        collate, ctype = database["datcollate"], database["datctype"]
        locale = database.get("datlocale", database.get("daticulocale"))
        rules, version, actual = database.get("daticurules"), database["datcollversion"], database["actual_version"]
    else:
        collate, ctype = raw["collcollate"], raw["collctype"]
        locale = raw.get("colllocale", raw.get("colliculocale"))
        rules, version = raw.get("collicurules"), raw["collversion"]
    if provider not in ("b", "c", "i") or version != actual:
        raise Unconfirmed("unsupported or stale collation provider/version")
    return [provider, raw["collisdeterministic"], database["encoding"], collate, ctype, locale, rules], version


@dataclass(frozen=True)
class _Catalog:
    table: sql.Identifier
    columns: list[str]
    primary_key: list[str]
    collatable: set[str]
    schema_hash: str
    schema_definition_hash: str


def _read_catalog(conn, table: str) -> _Catalog:
    parts = table.split(".")
    if len(parts) != 2 or not all(parts):
        raise Unconfirmed("comparison requires a schema-qualified table")
    identifier = sql.Identifier(*parts)
    row = conn.execute(_CATALOG, {"table": identifier.as_string(conn), "types": _SUPPORTED_TYPES}).fetchone()
    if row is None:
        raise Unconfirmed(f"comparison table missing: {table}")
    relkind, has_children, database, columns, constraints, primary_key = row
    if relkind != "r" or has_children:
        raise Unconfirmed(f"comparison requires a plain table: {table}")
    if not primary_key:
        raise Unconfirmed(f"comparison requires a primary key: {table}")
    if unsupported := [f"{c['name']} {c['type']}" for c in columns if not c["supported"]]:
        raise Unconfirmed(f"unsupported fingerprint type in {table}: {', '.join(unsupported)}")
    database_collation, database_version = _collation(
        {"collprovider": "d", "collisdeterministic": True}, None, database)
    described, versions = [], [database_version]
    for c in columns:
        collation, version = _collation(c["collation"], c["collation_actual"], database)
        described.append([c["name"], c["type"], c["not_null"], c["identity"], c["generated"], c["default"], collation])
        versions.append(version)
    definition = [database_collation, described, sorted(constraints), primary_key]
    return _Catalog(
        table=identifier,
        columns=sorted(c["name"] for c in columns),
        primary_key=primary_key,
        collatable={c["name"] for c in columns if c["collation"] is not None},
        # schema_hash는 제공자 버전까지 포함한다. 이 값만 비교하는 게이트(T19)도 버전 차이에서 통과하지 않는다.
        schema_hash=_sha256([definition, versions]),
        schema_definition_hash=_sha256(definition),
    )


def _row_hash(columns: list[str]) -> sql.Composable:
    values = sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(c)) for c in columns)
    return sql.SQL("sha256(convert_to(jsonb_build_array({})::text, 'UTF8'))").format(values)


def _fingerprint_query(catalog: _Catalog) -> sql.Composable:
    # 글자 PK는 DB마다 정렬이 다를 수 있어 바이트 순서로 고정한다. 행 지문은 32바이트로 길이가 같아 경계가 섞이지 않는다.
    order = sql.SQL(", ").join(
        sql.SQL('{} COLLATE "C"').format(sql.Identifier(c)) if c in catalog.collatable else sql.Identifier(c)
        for c in catalog.primary_key)
    return sql.SQL("""SELECT count(*),
  encode(sha256(coalesce(string_agg({pk}, ''::bytea ORDER BY {order}), ''::bytea)), 'hex'),
  encode(sha256(coalesce(string_agg({content}, ''::bytea ORDER BY {order}), ''::bytea)), 'hex')
FROM {table}""").format(pk=_row_hash(catalog.primary_key), content=_row_hash(catalog.columns),
                        order=order, table=catalog.table)


@contextmanager
def _snapshot(conn) -> Iterator[None]:
    """autocommit 연결 전용. 오류 문구에 행 값이 섞일 수 있어 영수증에는 SQLSTATE만 남긴다."""
    try:
        with conn.transaction():
            conn.execute(_SNAPSHOT)
            yield
    except psycopg.errors.QueryCanceled as e:
        raise Unconfirmed(f"fingerprint exceeded statement_timeout {STATEMENT_TIMEOUT_MS}ms") from e
    except psycopg.Error as e:
        raise Unconfirmed(f"integrity snapshot could not be read ({e.sqlstate})") from e


def schema_fingerprint(conn, table: str) -> dict:
    """게이트(T19)용. 데이터를 보지 않는다."""
    with _snapshot(conn):
        catalog = _read_catalog(conn, table)
    return {"schema_hash": catalog.schema_hash, "schema_definition_hash": catalog.schema_definition_hash,
            "primary_key": catalog.primary_key}


def fingerprint(conn, table: str) -> dict:
    with _snapshot(conn):
        catalog = _read_catalog(conn, table)
        rows, pk_hash, content_hash = conn.execute(_fingerprint_query(catalog)).fetchone()
    return {"rows": rows, "schema_hash": catalog.schema_hash, "schema_definition_hash": catalog.schema_definition_hash,
            "pk_hash": pk_hash, "content_hash": content_hash}


def schema_verdict(source: dict, target: dict) -> str:
    if source["schema_hash"] == target["schema_hash"]:
        return MATCH
    # 정의는 같은데 정렬 제공자 버전만 다르면 실제 정렬이 달라졌는지 알 수 없다
    if source["schema_definition_hash"] == target["schema_definition_hash"]:
        return UNKNOWN
    return MISMATCH


def _data_verdict(source: dict, target: dict) -> str:
    same = all(source[key] == target[key] for key in ("rows", "pk_hash", "content_hash"))
    return MATCH if same else MISMATCH


def _combined(verdicts: list[str]) -> str:
    if MISMATCH in verdicts:
        return MISMATCH
    return MATCH if verdicts and all(v == MATCH for v in verdicts) else UNKNOWN


def _compare_table(src, dst, table: str) -> dict:
    try:
        source, target = src.db_fingerprint(table), dst.db_fingerprint(table)
    except Unconfirmed as e:
        return {"name": table, "rows_src": None, "rows_dst": None, "source": None, "target": None, "match": False,
                "data_verdict": UNKNOWN, "schema_verdict": UNKNOWN, "error": str(e)}
    data, schema = _data_verdict(source, target), schema_verdict(source, target)
    return {"name": table, "rows_src": source["rows"], "rows_dst": target["rows"],
            "source": source, "target": target, "match": data == schema == MATCH,
            "data_verdict": data, "schema_verdict": schema}


class DbFingerprintComparer:
    """Cutover의 comparer 자리에 그대로 들어간다. 진행 여부는 verdict가 MATCH인지만으로 정한다."""

    hash_rule = HASH_RULE

    def compare(self, src, dst, tables: list[str]) -> dict:
        results = [_compare_table(src, dst, table) for table in tables]
        data = _combined([t["data_verdict"] for t in results])
        schema = _combined([t["schema_verdict"] for t in results])
        return {"hash_rule": self.hash_rule, "tables": results, "data_verdict": data, "schema_verdict": schema,
                "verdict": _combined([data, schema])}


@dataclass(frozen=True)
class SequenceState:
    last_value: int
    is_called: bool
    max_id: int  # 표의 실제 최대 ID. 논리 복제는 시퀀스를 옮기지 않아 last_value보다 클 수 있다.
    maximum: int

    @property
    def next_value(self) -> int:
        return self.last_value + 1 if self.is_called else self.last_value


def sequence_state(conn, sequence: str, table: str, column: str) -> SequenceState:
    identifier = sql.Identifier(*sequence.split("."))
    row = conn.execute(sql.SQL("""SELECT s.last_value, s.is_called, p.seqincrement, p.seqcycle, p.seqmax,
         (SELECT coalesce(max({column}), 0) FROM {table})
       FROM {sequence} s, pg_catalog.pg_sequence p WHERE p.seqrelid = to_regclass(%s)""").format(
        column=sql.Identifier(column), table=sql.Identifier(*table.split(".")), sequence=identifier),
        (identifier.as_string(conn),)).fetchone()
    if row is None:
        raise Unconfirmed(f"sequence settings not found: {sequence}")
    last_value, is_called, increment, cycle, maximum, max_id = row
    if increment != 1 or cycle:
        raise Unconfirmed(f"only ascending unit non-cycling sequences are supported: {sequence}")
    return SequenceState(last_value, is_called, max_id, maximum)


def next_safe_value(source: SequenceState, target: SequenceState) -> int:
    """양쪽에 이미 있거나 곧 발급될 번호와 겹치지 않고, 목적지의 다음 값보다 작아지지 않는 값."""
    return max(source.next_value, source.max_id + 1, target.next_value, target.max_id + 1)


def correct_sequence(src, dst, sequence: str, table: str, column: str) -> dict:
    """쓰기를 막은 뒤 부른다. 목적지 시퀀스를 올리기만 하고, 올린 값을 다시 읽어 확인한 영수증 항목을 돌려준다."""
    source = src.sequence_state(sequence, table, column)
    before = dst.sequence_state(sequence, table, column)
    wanted = next_safe_value(source, before)
    if wanted > before.maximum:
        raise Unconfirmed(f"sequence next value {wanted} exceeds its maximum {before.maximum}: {sequence}")
    if wanted > before.next_value:
        dst.raise_sequence(sequence, wanted)
    after = dst.sequence_state(sequence, table, column)
    if after.next_value != wanted:
        raise Unconfirmed(f"sequence next value is {after.next_value}, expected {wanted}: {sequence}")
    return {"name": sequence, "src_last": source.last_value, "src_is_called": source.is_called,
            "src_next": source.next_value, "src_max_id": source.max_id,
            "dst_last_before": before.last_value, "dst_is_called_before": before.is_called,
            "dst_next_before": before.next_value, "dst_max_id": before.max_id,
            "dst_set": after.next_value, "raised": after.next_value > before.next_value}
