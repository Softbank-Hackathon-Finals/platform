"""실행 기록 (spec D2): 플랫폼 DB의 executor 스키마. 멱등 키 · epoch · lease를 한 트랜잭션에서 판정한다.

lease와 epoch는 서버에 등록한 실제 DB 자원(resources.Registry)마다 건다. 출발지 · 목적지 자원을 정렬된
순서로 잠그고 함께 판정하므로, 같은 DB 쌍뿐 아니라 한쪽 DB만 겹치는 실행도 막힌다.

- 멱등 키: 같은 키 · 같은 입력이면 저장된 실행을 돌려주고, 입력이 다르면 거부한다.
- epoch: 두 자원 각각에서 받은 최댓값보다 크지 않으면 거부한다. 재시도는 같은 키로 한다.
- lease: 자원마다 PENDING · RUNNING · SAFE_HOLD인 실행은 하나뿐이다. SAFE_HOLD는 사람이 풀 때까지 쥔다.
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
from bravo_cutover.rejections import (  # noqa: F401 - 호출하는 쪽이 store에서 가져다 쓴다
    DeadlinePassed,
    IdempotencyConflict,
    LeaseHeld,
    Rejected,
    StaleEpoch,
)

SCHEMA_SQL = """
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
-- 실제 DB 자원 단위의 epoch와 lease
ALTER TABLE executor.executions ADD COLUMN IF NOT EXISTS resources text[];
CREATE TABLE IF NOT EXISTS executor.resource_fences (
    resource_id text   PRIMARY KEY,
    max_epoch   bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS executor.leases (
    resource_id  text PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES executor.executions (execution_id)
);
""".format(holding=", ".join(f"'{s}'" for s in LEASE_HOLDING))


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
            # Operators can pre-create this schema for a migrator without database-wide CREATE.
            # Even CREATE SCHEMA IF NOT EXISTS checks that broader privilege before its no-op.
            if conn.execute("SELECT to_regnamespace('executor') AS schema").fetchone()["schema"] is None:
                conn.execute("CREATE SCHEMA executor")
            conn.execute(SCHEMA_SQL)

    def create(self, key: str, request: CutoverRequest, resources: tuple[str, str]) -> tuple[Execution, bool]:
        """새 실행이면 (실행, True), 같은 키 재요청이면 (기존 실행, False).
        resources는 서버 등록 정보로 확인한 (출발지, 목적지) 실제 DB 자원 ID다(Registry.resolve)."""
        try:
            return self._create(key, request, resources)
        except errors.UniqueViolation:
            # 같은 키가 동시에 들어와 한쪽이 먼저 커밋했다. 저장된 것을 기준으로 다시 판정한다.
            return self._create(key, request, resources)

    def _create(self, key: str, request: CutoverRequest, resources: tuple[str, str]) -> tuple[Execution, bool]:
        locked = sorted(set(resources))
        scope, digest = "|".join(locked), request.digest()
        with self._pool.connection() as conn, conn.transaction():
            for resource_id in locked:
                conn.execute("INSERT INTO executor.resource_fences VALUES (%s, 0) ON CONFLICT DO NOTHING",
                             (resource_id,))
            # 자원마다 판정을 직렬화한다. 정렬된 순서로 잠가 교착을 피한다. 이 잠금 뒤에 멱등 키를 봐야
            # 같은 키 동시 요청이 epoch 거부로 새지 않는다.
            fences = conn.execute(
                """SELECT resource_id, max_epoch FROM executor.resource_fences
                   WHERE resource_id = ANY(%s) ORDER BY resource_id FOR UPDATE""",
                (locked,),
            ).fetchall()
            row = conn.execute(
                "SELECT * FROM executor.executions WHERE idempotency_key = %s", (key,)
            ).fetchone()
            if row:
                if row["request_digest"] != digest or sorted(row["resources"] or []) != locked:
                    raise IdempotencyConflict("same Idempotency-Key with a different request")
                return Execution.from_row(row), False
            if request.deadline_passed():
                raise DeadlinePassed(f"deadline {request.deadline.isoformat()} has passed")
            max_epoch = max(f["max_epoch"] for f in fences)
            if request.epoch <= max_epoch:
                raise StaleEpoch(f"epoch {request.epoch} <= {max_epoch}")
            holder = conn.execute(
                """SELECT l.resource_id, e.execution_id, e.status FROM executor.leases l
                   JOIN executor.executions e USING (execution_id)
                   WHERE l.resource_id = ANY(%s) AND e.status = ANY(%s)""",
                (locked, [s.value for s in LEASE_HOLDING]),
            ).fetchone()
            if holder:
                raise LeaseHeld(f"resource {holder['resource_id']} is held by execution "
                                f"{holder['execution_id']} ({holder['status']})")
            row = conn.execute(
                """INSERT INTO executor.executions
                   (execution_id, idempotency_key, request_digest, scope, resources, plan_id, epoch, request, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (uuid4(), key, digest, scope, list(resources), request.plan_id, request.epoch,
                 Jsonb(request.model_dump(mode="json")), Status.PENDING.value),
            ).fetchone()
            for resource_id in locked:
                conn.execute(
                    """INSERT INTO executor.leases VALUES (%s, %s)
                       ON CONFLICT (resource_id) DO UPDATE SET execution_id = EXCLUDED.execution_id""",
                    (resource_id, row["execution_id"]),
                )
            conn.execute("UPDATE executor.resource_fences SET max_epoch = %s WHERE resource_id = ANY(%s)",
                         (request.epoch, locked))
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
                    detail: str | None = None) -> bool:
        """단계 이벤트를 덧붙인다. T10은 이 목록을 execution.updated 이벤트로 바꾼다.
        이 프로세스가 맡은 RUNNING 실행일 때만 쓴다(넘겨받힌 뒤에는 False)."""
        event = {"step": step, "status": status}
        if elapsed_ms is not None:
            event["elapsed_ms"] = elapsed_ms
        if detail:
            event["detail"] = detail[:500]
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE executor.executions SET steps = steps || %s, updated_at = now()
                   WHERE execution_id = %s AND status = %s AND owner = %s RETURNING execution_id""",
                (Jsonb([event]), execution_id, Status.RUNNING.value, self.owner_id),
            ).fetchone()
        return row is not None

    def mark(self, execution_id: UUID, milestone: str) -> bool:
        """6단계 기록. 되돌릴 수 없는 지점이라 DB에 남은 뒤에만 다음 조작을 한다. 소유자일 때만 쓴다."""
        if milestone not in ("stage6_entered_at", "committed_at"):
            raise ValueError(milestone)
        with self._pool.connection() as conn:
            row = conn.execute(
                f"""UPDATE executor.executions SET {milestone} = now(), updated_at = now()
                    WHERE execution_id = %s AND status = %s AND owner = %s RETURNING execution_id""",
                (execution_id, Status.RUNNING.value, self.owner_id),
            ).fetchone()
        return row is not None

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
                """UPDATE executor.executions SET status = %s,
                   result = COALESCE(result, '{}'::jsonb) || %s, updated_at = now()
                   WHERE execution_id = %s AND status = %s AND owner = %s RETURNING execution_id""",
                (status.value, Jsonb(result), execution_id, Status.RUNNING.value, self.owner_id),
            ).fetchone()
        return row is not None

    def record_receipt(self, execution_id: UUID, receipt: dict[str, Any]) -> bool:
        """Persist comparison evidence before any target LOGIN, retaining it on failure/restart."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE executor.executions SET
                   result = COALESCE(result, '{}'::jsonb) || %s, updated_at = now()
                   WHERE execution_id = %s AND status = %s AND owner = %s RETURNING execution_id""",
                (Jsonb({"receipt": receipt}), execution_id, Status.RUNNING.value, self.owner_id),
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
