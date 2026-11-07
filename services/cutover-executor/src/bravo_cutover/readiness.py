"""컷오버 전 데이터 준비 관찰(T13 → T19 게이트 입력). 판정하지 않는다: 게이트가 한다.

등록된 두 환경의 DB를 실행기와 같은 제어 연결 · 같은 T13 함수로 읽는다.
- 스키마 · 기본 키: 표마다 두 번 읽고, 그 사이에 바뀌었거나 읽지 못하면 값을 비운다(T19 collect_schema와 같은 규칙).
- 복제 지연: 사전 확인과 같은 계산(출발지 WAL 위치 - 정방향 구독이 받은 위치). 구독이 돌지 않으면 비운다.
- 시퀀스: T13 계약 「소비자 연결」대로 readback_next는 correct_sequence가 돌려준 dst_set이다. 목적지 시퀀스는
  올리기만 하고, 목적지 앱 역할이 로그인할 수 없고 붙은 세션이 없음을 본 뒤에만 올린다. 아니면 행을 뺀다.
읽지 못한 값은 비우거나 행을 빼고 고정 코드만 남긴다. 예외 원문 · 접속 정보는 내보내지 않는다.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bravo_cutover.db_fingerprint import (
    HASH_RULE,
    FingerprintUnconfirmed,
    SequenceState,
    correct_sequence,
    next_safe_value,
)
from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.db import DbControl
from bravo_cutover.sites import Config

# 관찰 유효 시간의 상한. T19 collect_schema와 같다.
MAX_AGE_S = 30


class ReadinessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_environment_id: str = Field(min_length=1, max_length=128)
    target_environment_id: str = Field(min_length=1, max_length=128)
    tables: list[str] = Field(min_length=1, max_length=64)
    sequence_columns: list[tuple[str, str]] = Field(default=[], max_length=16)
    max_age_seconds: int = Field(MAX_AGE_S, ge=1, le=300)


class NotObservable(Exception):
    """요청을 관찰하지 않는다(등록되지 않은 환경 · 표 · 시퀀스, 시간 초과). code는 응답에 그대로 나간다."""

    def __init__(self, code: str, http_status: int):
        super().__init__(code)
        self.code, self.http_status = code, http_status


class Readiness:
    def __init__(self, config: Config, *, db: Callable[..., DbControl] = DbControl,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._config, self._db, self._clock = config, db, clock

    def observe(self, request: ReadinessRequest) -> dict[str, Any]:
        sites, settings = self._config.sites, self._config.settings
        src_site = sites.get(request.source_environment_id)
        dst_site = sites.get(request.target_environment_id)
        if src_site is None or dst_site is None:
            raise NotObservable("environment_not_registered", 404)
        try:
            self._config.direction(src_site.environment_id, dst_site.environment_id)
        except ValueError:
            raise NotObservable("direction_not_registered", 422) from None
        if len(set(request.tables)) != len(request.tables) or not set(request.tables) <= set(settings.tables):
            raise NotObservable("table_not_registered", 422)
        sequences = {tuple(pair): name for name, pair in settings.sequences.items()}
        pairs = [tuple(pair) for pair in request.sequence_columns]
        if len(set(pairs)) != len(pairs) or any(p not in sequences or p[0] not in request.tables for p in pairs):
            raise NotObservable("sequence_not_registered", 422)

        src = self._db(src_site.db_control_dsn, control_functions=src_site.control_functions)
        dst = self._db(dst_site.db_control_dsn, control_functions=dst_site.control_functions)
        started = self._clock()
        expires = started + timedelta(seconds=min(MAX_AGE_S, request.max_age_seconds))
        stamp = {"observed_at": started.isoformat(), "expires_at": expires.isoformat()}

        def side(site, db) -> dict[str, Any]:
            tables, errors = _schema(db, request.tables)
            return {"environment_id": site.environment_id, "database_ref": site.resource_id, **stamp,
                    "tables": tables, "errors": errors}

        source, target = side(src_site, src), side(dst_site, dst)
        lag, replication_error = _lag(src, dst, dst_site.subscription)
        rows, sequence_errors = _sequences(src, dst, dst_site.app_role, pairs, sequences, stamp)
        # 늦게 끝난 관찰은 받는 쪽에서 이미 지난 값이다. 일부만 돌려주지 않는다.
        if not started <= self._clock() < expires:
            raise NotObservable("readiness_capture_expired", 503)
        return {
            "kind": "t13-readiness-observation.v1", "schema_rule": HASH_RULE,
            "source": source, "target": target,
            "replication": {"source_database_ref": src_site.resource_id,
                            "target_database_ref": dst_site.resource_id, **stamp, "lag_bytes": lag},
            "replication_error": replication_error,
            "sequences": rows, "sequence_errors": sequence_errors,
        }


def _schema(db: DbControl, tables: list[str]) -> tuple[list[dict], dict[str, str]]:
    rows = {t: {"name": t, "schema_hash": None, "primary_key": None} for t in tables}
    errors: dict[str, str] = {}
    for table in tables:
        try:
            observed = db.schema_fingerprint(table)
            rows[table].update(schema_hash=observed["schema_hash"], primary_key=observed["primary_key"])
        except FingerprintUnconfirmed as e:
            errors[table] = e.code
    # 다시 읽어 그 사이의 스키마 변경을 잡는다
    for table in tables:
        if table in errors:
            continue
        try:
            again = db.schema_fingerprint(table)
            if any(again[k] != rows[table][k] for k in ("schema_hash", "primary_key")):
                raise FingerprintUnconfirmed("schema_changed")
        except FingerprintUnconfirmed as e:
            rows[table].update(schema_hash=None, primary_key=None)
            errors[table] = e.code
    return list(rows.values()), errors


def _lag(src: DbControl, dst: DbControl, subscription: str) -> tuple[int | None, str | None]:
    try:
        forward = dst.subscription(subscription)
        if not (forward.enabled and forward.workers):
            return None, "replication_not_running"
        received = dst.received_lsn(subscription)
        if received is None:
            return None, "replication_not_running"
        return int(src.current_wal_lsn()) - int(received), None
    except Unconfirmed:
        return None, "replication_unconfirmed"


def _sequences(src: DbControl, dst: DbControl, app_role: str, pairs: list[tuple[str, str]],
               names: dict[tuple[str, str], str], stamp: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    rows: list[dict] = []
    errors: dict[str, str] = {}
    if not pairs:
        return rows, errors
    try:
        closed = not dst.can_login(app_role) and dst.sessions(app_role) == 0
        reason = None if closed else "target_open"
    except Unconfirmed:
        closed, reason = False, "target_state_unconfirmed"
    for table, column in pairs:
        key = f"{table}:{column}"
        if not closed:
            errors[key] = reason
            continue
        try:
            r = correct_sequence(src, dst, names[(table, column)], table, column)
        except FingerprintUnconfirmed as e:
            errors[key] = e.code
            continue
        # 계약의 proposed_next = next_safe_value(출발지, 올리기 전 목적지). 영수증의 읽은 값으로 다시 계산한다
        # (최대치는 이 계산에 쓰이지 않는다). correct_sequence는 dst_set이 이 값과 같음을 확인했다.
        proposed = next_safe_value(SequenceState(r["src_last"], r["src_is_called"], r["src_max_id"], 0),
                                   SequenceState(r["dst_last_before"], r["dst_is_called_before"], r["dst_max_id"], 0))
        rows.append({"table": table, "column": column, "source_next": r["src_next"],
                     "target_next": r["dst_next_before"], "target_max_id": r["dst_max_id"],
                     "proposed_next": proposed, "readback_next": r["dst_set"],
                     "increment": 1, "cycle": False, **stamp})
    return rows, errors
