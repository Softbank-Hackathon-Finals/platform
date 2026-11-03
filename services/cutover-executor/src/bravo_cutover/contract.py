"""Executor 호출 계약 (설계 문서 8.6). 입력 · 출력 형식과 상태 값."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


class Status(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ABORT_PRE_WRITE = "ABORT_PRE_WRITE"
    SAFE_HOLD = "SAFE_HOLD"


# lease(동시에 한 실행만)를 쥐고 있는 상태. SAFE_HOLD는 사람이 풀기 전까지 다음 실행을 막는다.
LEASE_HOLDING = (Status.PENDING, Status.RUNNING, Status.SAFE_HOLD)


class StepStatus(StrEnum):
    """T10 execution.updated 이벤트의 status 값과 같다."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_id: Identifier
    # 접속 정보 참조(시크릿 이름 등). 값 자체는 받지 않는다.
    connection_ref: Annotated[str, Field(min_length=1, max_length=256)]


def _future(value: datetime) -> datetime:
    if value <= datetime.now(UTC):
        raise ValueError("deadline is in the past")
    return value


class CutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    source: Endpoint
    target: Endpoint
    write_pause_budget_ms: Annotated[int, Field(gt=0, le=600_000)]
    deadline: Annotated[AwareDatetime, AfterValidator(_future)]

    def scope(self) -> str:
        """같은 DB 쌍을 다루는 실행끼리 epoch · lease를 공유한다. 되돌리기(T20)도 같은 범위다."""
        a, b = sorted((self.source.environment_id, self.target.environment_id))
        return f"{a}|{b}"

    def digest(self) -> str:
        """같은 Idempotency-Key로 다른 입력이 왔는지 가리는 값."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()
