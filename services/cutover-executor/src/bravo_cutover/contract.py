"""Executor 호출 계약 (설계 문서 8.6). 입력 · 출력 형식과 상태 값."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]


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


class CutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: Identifier
    epoch: Annotated[int, Field(ge=1)]
    source: Endpoint
    target: Endpoint
    write_pause_budget_ms: Annotated[int, Field(gt=0, le=600_000)]
    # 지난 deadline은 새 실행에서만 거부한다(store.create). 저장된 요청을 다시 읽거나 같은 키로 재요청할 때는
    # 이미 지난 deadline이어도 그대로 읽혀야 상태를 돌려주고 재시작 판정을 할 수 있다.
    deadline: AwareDatetime

    def deadline_passed(self) -> bool:
        return self.deadline <= datetime.now(UTC)

    def digest(self) -> str:
        """같은 Idempotency-Key로 다른 입력이 왔는지 가리는 값."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()
