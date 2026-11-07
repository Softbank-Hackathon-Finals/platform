"""실행기 설정: 어느 큐를 어느 핸들러로 부르는지(이벤트 소스 매핑에 해당).

지원하지 않는 설정은 조용히 무시하지 않고 시작 전에 거부한다(ADR-0041). 거부 사유는 한 번에 모두 돌려준다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 이벤트 소스 매핑 · 함수 설정 가운데 이 런타임이 재현하지 않는 것. 키 → 사유.
UNSUPPORTED = {
    "filter_criteria": "이벤트 필터는 지원하지 않는다",
    "function_response_types": "배치 일부 실패 응답(ReportBatchItemFailures)은 지원하지 않는다",
    "maximum_batching_window_s": "배치 대기 시간은 지원하지 않는다(0초로 동작)",
    "scaling_config": "자동 확장은 재현하지 않는다(max_concurrency 고정값)",
    "destination_config": "실패 목적지는 지원하지 않는다(큐의 실패 보관 큐를 쓴다)",
}
BINDING_KEYS = {
    "function_name", "handler", "handler_path", "queue_name", "batch_size", "timeout_s",
    "max_concurrency", "memory_mb", "pass_env",
}
NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
HANDLER = re.compile(r"^[A-Za-z_][\w.]*\.[A-Za-z_]\w*$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Binding:
    function_name: str
    handler: str  # Lambda 형식 "모듈.함수" (예: "checks.handler")
    queue_name: str
    handler_path: str = "."
    batch_size: int = 10
    timeout_s: float = 3.0  # Lambda 기본값
    max_concurrency: int = 1
    memory_mb: int = 128  # context 표시용
    pass_env: tuple[str, ...] = ()  # 핸들러에 넘길 환경 변수 이름(값은 프로세스 환경에서 읽는다)

    @property
    def module(self) -> str:
        return self.handler.rsplit(".", 1)[0]

    @property
    def function(self) -> str:
        return self.handler.rsplit(".", 1)[1]


@dataclass(frozen=True)
class Config:
    bindings: tuple[Binding, ...]
    region: str = "ap-northeast-1"
    account_id: str = "000000000000"  # ARN 표시용. 실제 계정 ID를 넣지 않는다
    admin_port: int = 8080
    extra: dict = field(default_factory=dict)


def _binding(i: int, raw: object, problems: list[str]) -> Binding | None:
    where = f"bindings[{i}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: 객체가 아니다")
        return None
    for key in sorted(raw):
        if key in UNSUPPORTED:
            problems.append(f"{where}.{key}: 지원 안 함 — {UNSUPPORTED[key]}")
        elif key not in BINDING_KEYS:
            problems.append(f"{where}.{key}: 알 수 없는 설정")
    known = {k: v for k, v in raw.items() if k in BINDING_KEYS}
    for key in ("function_name", "handler", "queue_name"):
        if not isinstance(known.get(key), str):
            problems.append(f"{where}.{key}: 문자열이 필요하다")
    if problems and not all(isinstance(known.get(k), str) for k in ("function_name", "handler", "queue_name")):
        return None
    queue = known["queue_name"]
    if queue.endswith(".fifo"):
        problems.append(f"{where}.queue_name: 지원 안 함 — FIFO 큐는 지원하지 않는다")
    if not NAME.match(known["function_name"]) or not NAME.match(queue.removesuffix(".fifo")):
        problems.append(f"{where}: 함수 · 큐 이름은 영문 · 숫자 · '-' · '_' 80자 이하")
    if not HANDLER.match(known["handler"]):
        problems.append(f"{where}.handler: '모듈.함수' 형식이어야 한다")
    batch = known.get("batch_size", 10)
    if not isinstance(batch, int) or not 1 <= batch <= 10:
        problems.append(f"{where}.batch_size: 1~10 (SQS 한 번 수신 상한)")
    timeout = known.get("timeout_s", 3.0)
    if not isinstance(timeout, int | float) or not 1 <= timeout <= 900:
        problems.append(f"{where}.timeout_s: 1~900초 (Lambda 상한)")
    conc = known.get("max_concurrency", 1)
    if not isinstance(conc, int) or not 1 <= conc <= 32:
        problems.append(f"{where}.max_concurrency: 1~32")
    env = known.get("pass_env", [])
    if not isinstance(env, list) or not all(isinstance(e, str) and ENV_NAME.match(e) for e in env):
        problems.append(f"{where}.pass_env: 대문자 환경 변수 이름 목록")
        env = []
    return Binding(
        function_name=known["function_name"], handler=known["handler"], queue_name=queue,
        handler_path=str(known.get("handler_path", ".")), batch_size=batch if isinstance(batch, int) else 10,
        timeout_s=float(timeout) if isinstance(timeout, int | float) else 3.0,
        max_concurrency=conc if isinstance(conc, int) else 1, memory_mb=int(known.get("memory_mb", 128)),
        pass_env=tuple(env),
    )


def parse(raw: object) -> Config:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["설정 최상위는 객체여야 한다"])
    for key in sorted(set(raw) - {"bindings", "region", "account_id", "admin_port"}):
        problems.append(f"{key}: 알 수 없는 설정")
    items = raw.get("bindings")
    if not isinstance(items, list) or not items:
        raise ConfigError(problems + ["bindings: 하나 이상 필요하다"])
    bindings = [b for i, item in enumerate(items) if (b := _binding(i, item, problems))]
    names = [b.function_name for b in bindings]
    if len(set(names)) != len(names):
        problems.append("bindings: function_name이 겹친다")
    if problems:
        raise ConfigError(problems)
    return Config(bindings=tuple(bindings), region=str(raw.get("region", "ap-northeast-1")),
                  account_id=str(raw.get("account_id", "000000000000")),
                  admin_port=int(raw.get("admin_port", 8080)))


def load(path: str | Path) -> Config:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))


def check_queue(binding: Binding, attributes: dict[str, str]) -> list[str]:
    """실제 큐 속성과 대조한다. AWS도 같은 조건을 이벤트 소스 매핑 생성 때 검사한다."""
    problems = []
    if attributes.get("FifoQueue", "false").lower() == "true":
        problems.append(f"{binding.queue_name}: 지원 안 함 — FIFO 큐")
    visibility = float(attributes.get("VisibilityTimeout", "0"))
    if visibility < binding.timeout_s:
        problems.append(
            f"{binding.queue_name}: 다시 꺼낼 수 있게 되는 시간({visibility:g}초)이 함수 제한 시간"
            f"({binding.timeout_s:g}초)보다 짧다. 처리 중 메시지가 다른 실행기에 다시 나간다"
        )
    return problems
