"""창 요약 수신 (계약 3절 "소비자가 확인할 것").

- 계약 스키마를 지킨 창만 받는다.
- received_at은 받은 순간의 플랫폼 시각으로 덮어쓴다. 생산자 값은 버린다.
- 서버가 받기로 한 계획(is_active_plan)의 창만 받는다. 계획 목록은 호스트(T10 상태 DB)가 정한다.
- 같은 (plan_id, window.start)는 한 번만 받는다.
- 이미 받은 창보다 늦게 끝나지 않는 창은 받지 않는다. 오래된 정상 창을 다시 보내 장애를 가리지 못하게 한다.
- 끝 시각이 더 늦어도 기존 창과 겹치면 받지 않는다. 같은 요청을 여러 판정 창에 중복 계산하지 않는다.
- 받은 시각보다 MAX_CLOCK_AHEAD_SECONDS 넘게 미래에 끝나는 창은 받지 않는다. 위 규칙 때문에 그런 창 하나가
  그 시각까지 이후 창을 모두 막기 때문이다. 판정은 같은 창을 CLOCK_SKEW로 쓰지 않으므로 결과는 같다(UNKNOWN).

저장은 WindowStore로 분리한다. SqliteWindowStore는 참조 구현이고, 플랫폼 DB(T10)에 같은 인터페이스로 연결한다.
"""

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from bravo_observability.contract import ContractViolation, validate
from bravo_observability.sli import receive, validate_window_counts

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# 기준 프로필 demo-shift의 max_clock_skew_seconds와 같다(시험이 대조한다)
MAX_CLOCK_AHEAD_SECONDS = 2
# 참조 구현이 계획마다 남기는 기간(가장 늦은 창의 end 기준). 판정은 가장 늦은 창만 쓰고, 판정 기록이 창 원본을 담는다
RETENTION = timedelta(hours=1)


class IntakeRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WindowStore(Protocol):
    def insert_if_newer(self, plan_id: str, start: str, end: str, document: dict) -> str:
        """원자적으로 저장한다. 결과: stored · duplicate · out_of_order · overlapping_window."""

    def latest(self, plan_id: str) -> dict | None:
        """window.end가 가장 늦은 창."""


class SqliteWindowStore:
    """참조 구현. 시각 비교는 정규화한 UTC 마이크로초 정수로 한다(문자열 비교 아님).

    RETENTION보다 오래된 창은 지운다. 가장 늦은 창은 남으므로, 지운 창을 다시 보내도 out_of_order로 거부된다.
    """

    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS windows (plan_id TEXT NOT NULL, start_us INTEGER NOT NULL,"
            " end_us INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY (plan_id, start_us))"
        )

    @staticmethod
    def _us(value: str) -> int:
        return (datetime.fromisoformat(value) - _EPOCH) // timedelta(microseconds=1)

    def insert_if_newer(self, plan_id: str, start: str, end: str, document: dict) -> str:
        start_us, end_us = self._us(start), self._us(end)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute(
                    "SELECT 1 FROM windows WHERE plan_id=? AND start_us=?", (plan_id, start_us)
                ).fetchone():
                    result = "duplicate"
                else:
                    (latest,) = self._db.execute(
                        "SELECT MAX(end_us) FROM windows WHERE plan_id=?", (plan_id,)
                    ).fetchone()
                    if latest is not None and end_us <= latest:
                        result = "out_of_order"
                    elif latest is not None and start_us < latest:
                        result = "overlapping_window"
                    else:
                        self._db.execute(
                            "INSERT INTO windows VALUES (?,?,?,?)",
                            (plan_id, start_us, end_us, json.dumps(document, ensure_ascii=False)),
                        )
                        self._db.execute(
                            "DELETE FROM windows WHERE plan_id=? AND end_us < ?",
                            (plan_id, end_us - RETENTION // timedelta(microseconds=1)),
                        )
                        result = "stored"
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return result

    def latest(self, plan_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT document FROM windows WHERE plan_id=? ORDER BY end_us DESC LIMIT 1", (plan_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None


def accept(
    store: WindowStore, document: object, *, now: str, is_active_plan: Callable[[str], bool]
) -> dict:
    """받은 창을 검증 · 저장하고 저장한 문서를 돌려준다. 거부는 IntakeRejected(code)."""
    try:
        validate("SliWindow", document)
        validate_window_counts(document)
    except (ContractViolation, ValueError) as e:
        raise IntakeRejected("invalid_window") from e
    assert isinstance(document, dict)
    start, end = (datetime.fromisoformat(document["window"][k]) for k in ("start", "end"))
    if end <= start:
        raise IntakeRejected("invalid_window")
    if end - datetime.fromisoformat(now) > timedelta(seconds=MAX_CLOCK_AHEAD_SECONDS):
        raise IntakeRejected("clock_ahead")
    if is_active_plan(document["plan_id"]) is not True:
        raise IntakeRejected("plan_not_accepting_windows")
    stored = receive(document, now)
    result = store.insert_if_newer(
        stored["plan_id"], stored["window"]["start"], stored["window"]["end"], stored
    )
    if result != "stored":
        raise IntakeRejected(result)
    return stored
