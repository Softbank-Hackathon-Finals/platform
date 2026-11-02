import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

# 플랫폼 DB(T10)가 정해지기 전까지 SQLite에 둔다. 테이블과 메서드는 PostgreSQL로 옮길 수 있게 단순하게 유지한다.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    source         TEXT    NOT NULL,
    environment_id TEXT    NOT NULL,
    uid            TEXT    NOT NULL,
    class_uid      INTEGER NOT NULL,
    category       TEXT    NOT NULL,
    severity_id    INTEGER NOT NULL,
    status_id      INTEGER NOT NULL,
    time           INTEGER NOT NULL,
    collected_time INTEGER NOT NULL,
    finding        TEXT    NOT NULL,
    PRIMARY KEY (source, environment_id, uid)
);
CREATE INDEX IF NOT EXISTS findings_view ON findings (environment_id, category, status_id, severity_id);
CREATE TABLE IF NOT EXISTS raw (
    raw_sha256 TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    raw        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT    NOT NULL,
    environment_id TEXT    NOT NULL,
    received_time  INTEGER NOT NULL,
    reason         TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS source_status (
    source            TEXT    NOT NULL,
    environment_id    TEXT    NOT NULL,
    enabled           INTEGER NOT NULL,
    last_attempt_time INTEGER,
    last_success_time INTEGER,
    error_code        TEXT,
    cursor            TEXT,
    PRIMARY KEY (source, environment_id)
);
"""

OPEN_STATUSES = (1, 2)  # New, In Progress


@dataclass(frozen=True)
class SourceRow:
    source: str
    environment_id: str
    enabled: bool
    last_attempt_time: int | None
    last_success_time: int | None
    error_code: str | None
    cursor: str | None


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # 탐지 결과 ------------------------------------------------------------

    def upsert(self, finding: dict, masked_raw: dict | None = None) -> str:
        """탐지 결과를 저장한다. 처음이면 Create, 있던 것이면 Update, 해결되면 Close로 activity를 맞춘다."""
        b = finding["bravo"]
        key = (b["source"], b["environment_id"], finding["finding_info"]["uid"])
        with self._tx() as db:
            exists = db.execute(
                "SELECT 1 FROM findings WHERE source=? AND environment_id=? AND uid=?", key
            ).fetchone()
            activity = 3 if finding["status_id"] == 4 else (2 if exists else 1)
            finding = {
                **finding,
                "activity_id": activity,
                "type_uid": finding["class_uid"] * 100 + activity,
            }
            db.execute(
                "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (source, environment_id, uid) DO UPDATE SET "
                "class_uid=excluded.class_uid, category=excluded.category, severity_id=excluded.severity_id, "
                "status_id=excluded.status_id, time=excluded.time, collected_time=excluded.collected_time, "
                "finding=excluded.finding",
                (
                    *key,
                    finding["class_uid"],
                    b["category"],
                    finding["severity_id"],
                    finding["status_id"],
                    finding["time"],
                    b["collected_time"],
                    json.dumps(finding, ensure_ascii=False),
                ),
            )
            if masked_raw is not None:
                db.execute(
                    "INSERT OR IGNORE INTO raw VALUES (?,?,?)",
                    (
                        b["raw_sha256"],
                        b["source"],
                        json.dumps(masked_raw, ensure_ascii=False, sort_keys=True),
                    ),
                )
        return "created" if activity == 1 else "updated" if activity == 2 else "closed"

    def quarantine(self, source: str, environment_id: str, received_time: int, reason: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO quarantine (source, environment_id, received_time, reason) VALUES (?,?,?,?)",
                (source, environment_id, received_time, reason[:500]),
            )

    def findings(
        self,
        *,
        environment_id=None,
        category=None,
        min_severity=0,
        open_only=True,
        limit=100,
        offset=0,
    ) -> list[dict]:
        sql, args = "SELECT finding FROM findings WHERE severity_id >= ?", [min_severity]
        if environment_id:
            sql, args = sql + " AND environment_id = ?", [*args, environment_id]
        if category:
            sql, args = sql + " AND category = ?", [*args, category]
        if open_only:
            sql += f" AND status_id IN {OPEN_STATUSES}"
        sql += " ORDER BY severity_id DESC, time DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._db.execute(sql, [*args, limit, offset]).fetchall()
        return [json.loads(r["finding"]) for r in rows]

    def open_counts(self) -> dict[tuple[str, str], dict[int, int]]:
        """(환경, 범주) → {심각도: 미해결 건수}"""
        with self._lock:
            rows = self._db.execute(
                f"SELECT environment_id, category, severity_id, COUNT(*) AS n FROM findings "
                f"WHERE status_id IN {OPEN_STATUSES} GROUP BY environment_id, category, severity_id"
            ).fetchall()
        out: dict[tuple[str, str], dict[int, int]] = {}
        for r in rows:
            out.setdefault((r["environment_id"], r["category"]), {})[r["severity_id"]] = r["n"]
        return out

    def resolve_silent(self, source: str, before_time: int) -> int:
        """이벤트형 출처(Falco)에서 정해진 시간 동안 다시 나오지 않은 결과를 해결 처리한다."""
        with self._tx() as db:
            rows = db.execute(
                f"SELECT finding FROM findings WHERE source=? AND time < ? AND status_id IN {OPEN_STATUSES}",
                (source, before_time),
            ).fetchall()
            for r in rows:
                f = json.loads(r["finding"])
                f.update(status_id=4, activity_id=3, type_uid=f["class_uid"] * 100 + 3)
                db.execute(
                    "UPDATE findings SET status_id=4, finding=? WHERE source=? AND environment_id=? AND uid=?",
                    (
                        json.dumps(f, ensure_ascii=False),
                        source,
                        f["bravo"]["environment_id"],
                        f["finding_info"]["uid"],
                    ),
                )
        return len(rows)

    # 출처 수집 상태 -------------------------------------------------------

    def register_source(self, source: str, environment_id: str, enabled: bool = True) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO source_status (source, environment_id, enabled) VALUES (?,?,?) "
                "ON CONFLICT (source, environment_id) DO UPDATE SET enabled=excluded.enabled",
                (source, environment_id, int(enabled)),
            )

    def record_success(
        self, source: str, environment_id: str, now: int, cursor: str | None = None
    ) -> None:
        """끝까지 성공한 수집 또는 인증된 heartbeat. 커서는 여기서만 전진한다."""
        with self._tx() as db:
            db.execute(
                "UPDATE source_status SET last_attempt_time=?, last_success_time=?, error_code=NULL, "
                "cursor=COALESCE(?, cursor) WHERE source=? AND environment_id=?",
                (now, now, cursor, source, environment_id),
            )

    def record_failure(self, source: str, environment_id: str, now: int, error_code: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE source_status SET last_attempt_time=?, error_code=? WHERE source=? AND environment_id=?",
                (now, error_code, source, environment_id),
            )

    def sources(self) -> list[SourceRow]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM source_status ORDER BY environment_id, source"
            ).fetchall()
        return [
            SourceRow(
                r["source"],
                r["environment_id"],
                bool(r["enabled"]),
                r["last_attempt_time"],
                r["last_success_time"],
                r["error_code"],
                r["cursor"],
            )
            for r in rows
        ]

    def source(self, source: str, environment_id: str) -> SourceRow | None:
        return next(
            (
                s
                for s in self.sources()
                if s.source == source and s.environment_id == environment_id
            ),
            None,
        )
