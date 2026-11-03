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
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope, epoch)
);
CREATE UNIQUE INDEX IF NOT EXISTS executions_one_lease_per_scope
    ON executor.executions (scope) WHERE status IN ({holding});
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
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Store:
    def __init__(self, conninfo: str, *, max_size: int = 8):
        self._pool = ConnectionPool(conninfo, min_size=1, max_size=max_size, open=True,
                                    kwargs={"row_factory": dict_row})

    def close(self) -> None:
        self._pool.close()

    def migrate(self) -> None:
        with self._pool.connection() as conn:
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
