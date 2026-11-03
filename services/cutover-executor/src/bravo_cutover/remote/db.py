"""DB 조작: 제어 역할(cutover_control)로 펜싱 · barrier · 구독 · 시퀀스, 앱 역할로 쓰기 시험.

필요한 권한은 testing/sql/10-roles.sql · 30-cutover.sql에 있다. 모든 연결은 짧은 시간 제한을 건다.
"""

import time
import uuid
from dataclasses import dataclass

import psycopg
from psycopg import sql

from bravo_cutover.db_fingerprint import SequenceState, fingerprint, sequence_state
from bravo_cutover.remote import Unconfirmed

_TIMEOUTS = "-c statement_timeout=3000 -c lock_timeout=1000"


class DbControl:
    def __init__(self, dsn: str, *, control_functions: bool = False):
        self._dsn = dsn
        self._control_functions = control_functions

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._dsn, autocommit=True, connect_timeout=3, options=_TIMEOUTS)
        except psycopg.Error as e:
            raise Unconfirmed(f"control connection: {e}") from e

    def _one(self, query, params=()):
        try:
            with self._connect() as conn:
                row = conn.execute(query, params).fetchone()
                return row[0] if row else None
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e

    def _do(self, *statements) -> None:
        try:
            with self._connect() as conn:
                for statement in statements:
                    conn.execute(statement)
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e

    # 펜싱 ---------------------------------------------------------------
    def can_login(self, role: str) -> bool:
        value = self._one("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role,))
        if value is None:
            raise Unconfirmed(f"role {role} not found")
        return value

    def set_login(self, role: str, allowed: bool) -> None:
        if self._control_functions:
            if role != "chat_app":
                raise Unconfirmed("control functions only manage chat_app")
            self._one("SELECT cutover.set_app_login(%s)", (allowed,))
        else:
            self._do(sql.SQL("ALTER ROLE {} {}").format(sql.Identifier(role), sql.SQL("LOGIN" if allowed else "NOLOGIN")))
        if self.can_login(role) != allowed:
            raise Unconfirmed(f"role {role} login is not {allowed}")

    def sessions(self, role: str) -> int:
        return self._one("SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (role,))

    def terminate_sessions(self, role: str) -> int:
        if self._control_functions:
            if role != "chat_app":
                raise Unconfirmed("control functions only manage chat_app")
            return self._one("SELECT cutover.terminate_app_sessions()")
        return self._one(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename = %s", (role,)
        )

    def long_transactions(self, role: str, older_than_ms: int) -> int:
        return self._one(
            """SELECT count(*) FROM pg_stat_activity
               WHERE usename = %s AND xact_start < now() - make_interval(secs => %s / 1000.0)""",
            (role, older_than_ms),
        )

    def other_sessions(self, system_roles: list[str]) -> list[str]:
        """업무 역할이 아닌데도 남아 있는 클라이언트 세션(우회 쓰기 후보: worker · CronJob · 직접 연결)."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT DISTINCT usename FROM pg_stat_activity
                       WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
                         AND NOT (usename = ANY(%s))""",
                    (system_roles,),
                ).fetchall()
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e
        return sorted(r[0] for r in rows)

    # 복제 위치 ------------------------------------------------------------
    def current_wal_lsn(self) -> int:
        return self._one("SELECT pg_current_wal_lsn() - '0/0'::pg_lsn")

    def received_lsn(self, subscription: str) -> int | None:
        """구독이 발행 쪽에서 받아 반영을 보고한 위치(발행 쪽 WAL 기준)."""
        return self._one(
            """SELECT latest_end_lsn - '0/0'::pg_lsn FROM pg_stat_subscription
               WHERE subname = %s AND relid IS NULL""",
            (subscription,),
        )

    # barrier ------------------------------------------------------------
    def insert_barrier(self, barrier_id: uuid.UUID, epoch: int) -> None:
        self._do(sql.SQL("INSERT INTO cutover.barrier (barrier_id, epoch) VALUES ({}, {})").format(
            sql.Literal(barrier_id), sql.Literal(epoch)))

    def has_barrier(self, barrier_id: uuid.UUID) -> bool:
        return self._one("SELECT count(*) FROM cutover.barrier WHERE barrier_id = %s", (barrier_id,)) == 1

    # 구독 ---------------------------------------------------------------
    def subscription(self, name: str) -> "Subscription":
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT s.subenabled, count(w.pid) FILTER (WHERE w.pid IS NOT NULL)
                       FROM pg_subscription s LEFT JOIN pg_stat_subscription w ON w.subid = s.oid
                       WHERE s.subname = %s GROUP BY s.subenabled""",
                    (name,),
                ).fetchone()
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e
        if row is None:
            raise Unconfirmed(f"subscription {name} not found")
        return Subscription(enabled=row[0], workers=row[1])

    def set_subscription(self, name: str, enabled: bool, owner: str, wait_s: float = 5) -> None:
        """켜고 끈 뒤 적용 작업자까지 그 상태가 됐는지 기다린다(끄면 작업자가 멈춰야 한다)."""
        if self._control_functions:
            self._one("SELECT cutover.set_subscription(%s, %s)", (name, enabled))
        else:
            self._do(
                sql.SQL("SET ROLE {}").format(sql.Identifier(owner)),
                sql.SQL("ALTER SUBSCRIPTION {} {}").format(sql.Identifier(name), sql.SQL("ENABLE" if enabled else "DISABLE")),
            )
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            sub = self.subscription(name)
            if sub.enabled == enabled and (sub.workers > 0) == enabled:
                return
            time.sleep(0.1)
        raise Unconfirmed(f"subscription {name} did not become enabled={enabled}")

    # 시퀀스 · 비교 --------------------------------------------------------
    def sequence_next(self, sequence: str) -> int:
        """다음에 나올 값. 아직 쓰지 않은 시퀀스면 last_value 자체다."""
        try:
            with self._connect() as conn:
                last, called = conn.execute(
                    sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(*sequence.split(".")))
                ).fetchone()
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e
        return last + 1 if called else last

    def raise_sequence(self, sequence: str, next_value: int) -> int:
        """다음 값이 next_value 이상이 되게 올린다. 내리지 않는다(T13 "시퀀스는 올리기만")."""
        if self._control_functions:
            if sequence != "public.messages_id_seq":
                raise Unconfirmed("control functions only manage messages_id_seq")
            return self._one("SELECT cutover.raise_messages_sequence(%s)", (next_value,))
        current = self.sequence_next(sequence)
        if current < next_value:
            self._one("SELECT setval(%s, %s, false)", (sequence, next_value))
        return max(current, next_value)

    def max_value(self, table: str, column: str) -> int:
        return self._one(sql.SQL("SELECT coalesce(max({}), 0) FROM {}").format(
            sql.Identifier(column), sql.Identifier(*table.split("."))))

    def sequence_state(self, sequence: str, table: str, column: str) -> SequenceState:
        try:
            with self._connect() as conn:
                return sequence_state(conn, sequence, table, column)
        except psycopg.Error as e:
            raise Unconfirmed(str(e)) from e

    def db_fingerprint(self, table: str) -> dict:
        try:
            with self._connect() as conn:
                return fingerprint(conn, table)
        except psycopg.Error as e:
            raise Unconfirmed(f"integrity snapshot could not be read ({e.sqlstate})") from e

    def row_count(self, table: str) -> int:
        return self._one(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(*table.split("."))))

    def fingerprint(self, table: str) -> dict:
        from bravo_cutover.integrity import fingerprint

        try:
            with self._connect() as conn:
                return fingerprint(conn, table)
        except psycopg.Error as e:
            raise Unconfirmed("integrity snapshot could not be read") from e


@dataclass(frozen=True)
class Subscription:
    enabled: bool
    workers: int


class WriteRefusal:
    REFUSED = "refused"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


# 로그인 거부(28000 invalid_authorization_specification) · 권한 없음(42501) · 읽기 전용(25006)
_REFUSED_STATES = {"28000", "28P01", "42501", "25006"}


def app_write(dsn: str, path: str, timeout_s: float = 3) -> float:
    """앱 역할로 cutover.probe에 한 행을 쓰고 응답(ACK)까지 걸린 초를 돌려준다."""
    start = time.monotonic()
    with psycopg.connect(dsn, autocommit=True, connect_timeout=max(1, int(timeout_s)),
                         options=f"-c statement_timeout={int(timeout_s * 1000)}") as conn:
        conn.execute("INSERT INTO cutover.probe (probe_id, path) VALUES (%s, %s)", (uuid.uuid4(), path))
    return time.monotonic() - start


def try_app_write(dsn: str, path: str, timeout_s: float = 3) -> str:
    """쓰기 시험: refused(거부 확인) · accepted(쓰기 성공) · unknown(연결 실패 등 판단 불가)."""
    try:
        app_write(dsn, path, timeout_s)
        return WriteRefusal.ACCEPTED
    except psycopg.Error as e:
        state = getattr(e, "sqlstate", None) or _sqlstate_from_message(str(e))
        return WriteRefusal.REFUSED if state in _REFUSED_STATES else WriteRefusal.UNKNOWN


def _sqlstate_from_message(message: str) -> str | None:
    # 접속 단계 오류는 sqlstate가 비어 올 수 있다. 서버가 보낸 문구로 로그인 거부만 가린다.
    if "is not permitted to log in" in message:
        return "28000"
    return None
