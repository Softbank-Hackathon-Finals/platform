"""실행 기록 (spec D2): 플랫폼 DB의 executor 스키마. 멱등 키 · epoch · lease를 한 트랜잭션에서 판정한다.

- 멱등 키: 같은 키 · 같은 입력이면 저장된 실행을 돌려주고, 입력이 다르면 거부한다.
- epoch: 범위(DB 쌍)마다 받은 최댓값보다 크지 않으면 거부한다. 재시도는 같은 키로 한다.
- lease: 범위마다 PENDING · RUNNING · SAFE_HOLD인 실행은 하나뿐이다(부분 유니크 인덱스).
지난 deadline은 새 실행에서만 거부한다(같은 키 재요청은 저장된 실행을 돌려준다).
거부는 모두 원격 조작 전에 끝난다. 실행기(runner)는 기록이 커밋된 뒤에만 불린다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from bravo_cutover.contract import LEASE_HOLDING, CutoverRequest, Status

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS executor;
CREATE TABLE IF NOT EXISTS executor.executions (
    execution_id    uuid        PRIMARY KEY,
    idempotency_key text        NOT NULL UNIQUE,
    request_digest  text        NOT NULL,
    scope           text        NOT NULL,
    plan_id         text        NOT NULL,
    epoch           bigint      NOT NULL,
    request         jsonb       NOT NULL,
    status          text        NOT NULL,
    steps           jsonb       NOT NULL DEFAULT '[]',
    result          jsonb,
    -- 6단계: 구독을 바꾸기 전(stage6_entered_at), 전환 확정(committed_at, 되돌릴 수 없는 지점)
    stage6_entered_at timestamptz,
    committed_at    timestamptz,
    -- 지금 이 실행을 진행하는 Executor 프로세스. 재시작 판정이 넘겨받으면 바뀌고, 앞 프로세스는 멈춘다.
    owner           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope, epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS executions_one_lease_per_scope
    ON executor.executions (scope) WHERE status IN ({holding});
-- 앞 버전에서 만든 테이블에 나중에 추가한 열 (CREATE TABLE IF NOT EXISTS는 열을 더하지 않는다)
ALTER TABLE executor.executions ADD COLUMN IF NOT EXISTS stage6_entered_at timestamptz;
ALTER TABLE executor.executions ADD COLUMN IF NOT EXISTS committed_at timestamptz;
ALTER TABLE executor.executions ADD COLUMN IF NOT EXISTS owner text;
CREATE TABLE IF NOT EXISTS executor.fences (
    scope     text   PRIMARY KEY,
    max_epoch bigint NOT NULL
);
""".format(holding=", ".join(f"'{s}'" for s in LEASE_HOLDING))


class Rejected(Exception):
    """원격 조작 없이 거부한 요청. code는 API 오류 코드, http_status는 응답 코드다."""

    code = "rejected"
    http_status = 409


class IdempotencyConflict(Rejected):
    code = "idempotency_key_conflict"


class StaleEpoch(Rejected):
    code = "stale_epoch"


class LeaseHeld(Rejected):
    code = "lease_held"


class DeadlinePassed(Rejected):
    code = "deadline_passed"
    http_status = 422


@dataclass(frozen=True)
class Execution:
    execution_id: UUID
    idempotency_key: str
    scope: str
    plan_id: str
    epoch: int
    request: dict[str, Any]
    status: Status
    steps: list[dict[str, Any]]
    result: dict[str, Any] | None
    stage6_entered_at: datetime | None
    committed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Execution":
        return cls(**{k: row[k] for k in cls.__dataclass_fields__} | {"status": Status(row["status"])})

    def view(self) -> dict[str, Any]:
        return {
            "execution_id": str(self.execution_id),
            "plan_id": self.plan_id,
            "epoch": self.epoch,
            "status": self.status.value,
            "steps": self.steps,
            "result": self.result,
            "committed": self.committed_at is not None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Store:
    def __init__(self, conninfo: str, *, max_size: int = 8):
        self._conninfo = conninfo
        self.owner_id = str(uuid4())
        self._pool = ConnectionPool(conninfo, min_size=1, max_size=max_size, open=True,
                                    kwargs={"row_factory": dict_row})

    def close(self) -> None:
        if getattr(self, "_singleton", None) is not None:
            self._singleton.close()
        self._pool.close()

    def migrate(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(SCHEMA_SQL)

    def create(self, key: str, request: CutoverRequest) -> tuple[Execution, bool]:
        """새 실행이면 (실행, True), 같은 키 재요청이면 (기존 실행, False)."""
        try:
            return self._create(key, request)
        except errors.UniqueViolation:
            # 같은 키가 동시에 들어와 한쪽이 먼저 커밋했다. 저장된 것을 기준으로 다시 판정한다.
            return self._create(key, request)

    def _create(self, key: str, request: CutoverRequest) -> tuple[Execution, bool]:
        scope, digest = request.scope(), request.digest()
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("INSERT INTO executor.fences VALUES (%s, 0) ON CONFLICT DO NOTHING", (scope,))
            # 범위마다 판정을 직렬화한다. 이 잠금 뒤에 멱등 키를 봐야 같은 키 동시 요청이 epoch 거부로 새지 않는다.
            max_epoch = conn.execute(
                "SELECT max_epoch FROM executor.fences WHERE scope = %s FOR UPDATE", (scope,)
            ).fetchone()["max_epoch"]
            row = conn.execute(
                "SELECT * FROM executor.executions WHERE idempotency_key = %s", (key,)
            ).fetchone()
            if row:
                if row["request_digest"] != digest:
                    raise IdempotencyConflict("same Idempotency-Key with a different request")
                return Execution.from_row(row), False
            if request.deadline_passed():
                raise DeadlinePassed(f"deadline {request.deadline.isoformat()} has passed")
            if request.epoch <= max_epoch:
                raise StaleEpoch(f"epoch {request.epoch} <= {max_epoch}")
            holder = conn.execute(
                "SELECT execution_id, status FROM executor.executions WHERE scope = %s AND status = ANY(%s)",
                (scope, [s.value for s in LEASE_HOLDING]),
            ).fetchone()
            if holder:
                raise LeaseHeld(f"execution {holder['execution_id']} is {holder['status']}")
            row = conn.execute(
                """INSERT INTO executor.executions
                   (execution_id, idempotency_key, request_digest, scope, plan_id, epoch, request, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (uuid4(), key, digest, scope, request.plan_id, request.epoch,
                 Jsonb(request.model_dump(mode="json")), Status.PENDING.value),
            ).fetchone()
            conn.execute("UPDATE executor.fences SET max_epoch = %s WHERE scope = %s", (request.epoch, scope))
            return Execution.from_row(row), True

    def get(self, execution_id: UUID) -> Execution | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM executor.executions WHERE execution_id = %s", (execution_id,)
            ).fetchone()
        return Execution.from_row(row) if row else None

    def find(self, plan_id: str, epoch: int) -> list[Execution]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM executor.executions WHERE plan_id = %s AND epoch = %s ORDER BY created_at",
                (plan_id, epoch),
            ).fetchall()
        return [Execution.from_row(r) for r in rows]

    def set_status(self, execution_id: UUID, status: Status, result: dict[str, Any] | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE executor.executions SET status = %s, result = COALESCE(%s, result), updated_at = now()
                   WHERE execution_id = %s""",
                (status.value, Jsonb(result) if result is not None else None, execution_id),
            )

    def record_step(self, execution_id: UUID, step: str, status: str, elapsed_ms: int | None = None,
                    detail: str | None = None) -> None:
        """단계 이벤트를 덧붙인다. T10은 이 목록을 execution.updated 이벤트로 바꾼다."""
        event = {"step": step, "status": status}
        if elapsed_ms is not None:
            event["elapsed_ms"] = elapsed_ms
        if detail:
            event["detail"] = detail[:500]
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE executor.executions SET steps = steps || %s, updated_at = now()
                   WHERE execution_id = %s""",
                (Jsonb([event]), execution_id),
            )

    def mark(self, execution_id: UUID, milestone: str) -> None:
        """6단계 기록. 되돌릴 수 없는 지점이라 DB에 남은 뒤에만 다음 조작을 한다."""
        if milestone not in ("stage6_entered_at", "committed_at"):
            raise ValueError(milestone)
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE executor.executions SET {milestone} = now(), updated_at = now() WHERE execution_id = %s",
                (execution_id,),
            )

    def unfinished(self) -> list[Execution]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM executor.executions WHERE status = ANY(%s) ORDER BY created_at",
                ([Status.PENDING.value, Status.RUNNING.value],),
            ).fetchall()
        return [Execution.from_row(r) for r in rows]

    def acquire_singleton(self) -> bool:
        """Executor 프로세스를 하나로 제한한다. 잠금은 이 연결이 살아 있는 동안 유지되고, 프로세스가 죽으면 풀린다."""
        if getattr(self, "_singleton", None) is not None:
            return True
        conn = psycopg.connect(self._conninfo, autocommit=True)
        if conn.execute("SELECT pg_try_advisory_lock(hashtext('bravo_cutover_executor'))").fetchone()[0]:
            self._singleton = conn
            return True
        conn.close()
        return False

    # 실행 소유 ------------------------------------------------------------
    def claim(self, execution_id: UUID, *, takeover: bool = False) -> bool:
        """이 프로세스가 실행을 맡는다. 처음 시작은 PENDING → RUNNING, 재시작 판정은 RUNNING을 넘겨받는다."""
        from_status = Status.RUNNING if takeover else Status.PENDING
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE executor.executions SET status = %s, owner = %s, updated_at = now()
                   WHERE execution_id = %s AND status = %s RETURNING execution_id""",
                (Status.RUNNING.value, self.owner_id, execution_id, from_status.value),
            ).fetchone()
        return row is not None

    def owns(self, execution_id: UUID) -> bool:
        """아직 이 프로세스가 맡고 있는가: 실행이 RUNNING이고 소유자가 나이며, 프로세스 잠금을 쥐었다면 그 잠금이 살아 있다."""
        if getattr(self, "_singleton", None) is not None and not self.singleton_alive():
            return False
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT status, owner FROM executor.executions WHERE execution_id = %s", (execution_id,)
                ).fetchone()
        except psycopg.Error:
            return False
        return row is not None and row["status"] == Status.RUNNING.value and row["owner"] == self.owner_id

    def finish(self, execution_id: UUID, status: Status, result: dict[str, Any]) -> bool:
        """소유자일 때만 최종 상태를 쓴다. 넘겨받힌 실행이면 아무것도 바꾸지 않고 False."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE executor.executions SET status = %s, result = %s, updated_at = now()
                   WHERE execution_id = %s AND status = %s AND owner = %s RETURNING execution_id""",
                (status.value, Jsonb(result), execution_id, Status.RUNNING.value, self.owner_id),
            ).fetchone()
        return row is not None

    def singleton_alive(self) -> bool:
        try:
            return self._singleton.execute(
                """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted
                                  AND pid = pg_backend_pid())"""
            ).fetchone()[0]
        except psycopg.Error:
            return False
