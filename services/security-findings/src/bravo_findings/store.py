import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from bravo_findings.mask import source_projection

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


class EventConflict(ValueError):
    """동일 출처 시각에 다른 판정. 선착순으로 해결하지 않고 확인 불가로 유지한다."""


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
        if path != ":memory:":
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid not in {0, os.getuid()}
                ):
                    raise ValueError("findings database must be a private regular file")
            finally:
                os.close(fd)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA synchronous=FULL")
        tables = {
            r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "findings" in tables and "security_schema" not in tables:
            for table in ("findings", "raw", "quarantine", "source_status"):
                if (
                    table in tables
                    and self._db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                ):
                    self._db.close()
                    raise ValueError("legacy findings DB requires reviewed export and re-ingestion")
        if "security_schema" in tables:
            versions = {r[0] for r in self._db.execute("SELECT version FROM security_schema")}
            if versions != {2}:
                self._db.close()
                raise ValueError("unsupported findings database schema version")
        self._db.executescript(_SCHEMA)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS security_schema (version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO security_schema VALUES (2);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL,
                request_id TEXT NOT NULL, subject TEXT, action TEXT NOT NULL,
                outcome TEXT NOT NULL, code TEXT NOT NULL, environment_id TEXT, source TEXT
            );
            CREATE TABLE IF NOT EXISTS finding_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                environment_id TEXT NOT NULL, uid TEXT NOT NULL, event_time INTEGER NOT NULL,
                received_time INTEGER NOT NULL, outcome TEXT NOT NULL, digest TEXT NOT NULL
            );
        """)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._db.in_transaction:
                yield self._db
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def batch(self):
        """결과·격리·수집 상태·커서를 한 트랜잭션으로 반영한다."""
        return self._tx()

    def audit(
        self, now, request_id, subject, action, outcome, code, environment_id=None, source=None
    ):
        with self._tx() as db:
            db.execute(
                "INSERT INTO audit (time,request_id,subject,action,outcome,code,environment_id,source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now, request_id, subject, action, outcome, code, environment_id, source),
            )

    @staticmethod
    def _semantic(finding):
        doc = json.loads(json.dumps(finding))
        for field in ("activity_id", "type_uid"):
            doc.pop(field, None)
        doc["bravo"].pop("collected_time", None)
        doc["bravo"].pop("raw_sha256", None)
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    # 탐지 결과 ------------------------------------------------------------

    def upsert(self, finding: dict, masked_raw: dict | None = None) -> str:
        """탐지 결과를 저장한다. 처음이면 Create, 있던 것이면 Update, 해결되면 Close로 activity를 맞춘다."""
        b = finding["bravo"]
        key = (b["source"], b["environment_id"], finding["finding_info"]["uid"])
        with self._tx() as db:
            exists = db.execute(
                "SELECT time, finding FROM findings WHERE source=? AND environment_id=? AND uid=?",
                key,
            ).fetchone()
            semantic = self._semantic(finding)
            digest = hashlib.sha256(semantic.encode()).hexdigest()
            outcome = "accepted"
            if exists and finding["time"] < exists["time"]:
                outcome = "stale"
            elif exists and finding["time"] == exists["time"]:
                if semantic != self._semantic(json.loads(exists["finding"])):
                    raise EventConflict("EVENT_VERSION_CONFLICT")
                outcome = "duplicate"
            db.execute(
                "INSERT INTO finding_history "
                "(source,environment_id,uid,event_time,received_time,outcome,digest) VALUES (?,?,?,?,?,?,?)",
                (*key, finding["time"], b["collected_time"], outcome, digest),
            )
            if outcome != "accepted":
                return outcome
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
                        json.dumps(
                            source_projection(b["source"], masked_raw),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
        return "created" if activity == 1 else "updated" if activity == 2 else "closed"

    def quarantine(self, source: str, environment_id: str, received_time: int, reason: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO quarantine (source, environment_id, received_time, reason) VALUES (?,?,?,?)",
                (
                    source,
                    environment_id,
                    received_time,
                    reason
                    if reason
                    in {"NORMALIZATION_FAILED", "EVENT_VERSION_CONFLICT", "INVALID_EVENT_TIME"}
                    else "NORMALIZATION_FAILED",
                ),
            )

    def findings(
        self,
        *,
        environment_id=None,
        environments=None,
        category=None,
        min_severity=0,
        open_only=True,
        limit=100,
        offset=0,
    ) -> list[dict]:
        sql, args = "SELECT finding FROM findings WHERE severity_id >= ?", [min_severity]
        if environments is not None:
            if not environments:
                return []
            sql += " AND environment_id IN (" + ",".join("?" for _ in environments) + ")"
            args.extend(sorted(environments))
        if environment_id:
            sql, args = sql + " AND environment_id = ?", [*args, environment_id]
        if category:
            sql, args = sql + " AND category = ?", [*args, category]
        if open_only:
            sql += f" AND status_id IN {OPEN_STATUSES}"
        sql += " ORDER BY severity_id DESC, time DESC, source, environment_id, uid LIMIT ? OFFSET ?"
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
        """침묵은 해결 증거가 아니다. 기존 내부 호출도 안전하게 실패시킨다."""
        raise ValueError("SILENCE_IS_NOT_RESOLUTION")

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
            if db.execute(
                "SELECT 1 FROM quarantine WHERE source=? AND environment_id=? LIMIT 1",
                (source, environment_id),
            ).fetchone():
                self.record_failure(source, environment_id, now, "INGESTION_GAP")
                return
            db.execute(
                "UPDATE source_status SET last_attempt_time=?, last_success_time=?, error_code=NULL, "
                "cursor=COALESCE(?, cursor) WHERE source=? AND environment_id=? "
                "AND (last_attempt_time IS NULL OR last_attempt_time <= ?)",
                (now, now, cursor, source, environment_id, now),
            )

    def record_failure(self, source: str, environment_id: str, now: int, error_code: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE source_status SET last_attempt_time=?, error_code=? WHERE source=? AND environment_id=? "
                "AND (last_attempt_time IS NULL OR last_attempt_time <= ?)",
                (now, error_code, source, environment_id, now),
            )

    def sources(self) -> list[SourceRow]:
        with self._lock:
            rows = self._db.execute(
                "SELECT s.*, EXISTS(SELECT 1 FROM quarantine q WHERE q.source=s.source "
                "AND q.environment_id=s.environment_id) AS has_gap "
                "FROM source_status s ORDER BY environment_id, source"
            ).fetchall()
        return [
            SourceRow(
                r["source"],
                r["environment_id"],
                bool(r["enabled"]),
                r["last_attempt_time"],
                r["last_success_time"],
                "INGESTION_GAP" if r["has_gap"] else r["error_code"],
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
