"""부품 ②: 호출 관리. 핸들러를 하위 프로세스에서 부르고 제한 시간을 넘기면 프로세스를 끝낸다.

같은 프로세스 안의 호출은 제한 시간에 끊을 수 없어서 하위 프로세스를 쓴다(T46 spec D4).
호출마다 새 프로세스라 Lambda의 콜드 스타트와 같은 조건이다. 1단계 · 2단계 모두 이 부품을 그대로 쓴다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum

from .config import Binding

LOG_LIMIT = 4000


class Kind(StrEnum):
    OK = "ok"
    ERROR = "error"  # 핸들러가 예외를 냈다
    TIMEOUT = "timeout"  # 제한 시간 초과로 종료했다
    CRASH = "crash"  # 결과 없이 프로세스가 끝났다


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    duration_ms: int
    error: str = ""
    logs: str = ""


def child_env(binding: Binding, region: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Lambda가 넣어 주는 환경 변수와, 설정에서 넘기기로 한 값만 둔다. 실행기 자신의 비밀은 넘기지 않는다."""
    environ = os.environ if environ is None else environ
    env = {
        "PATH": environ.get("PATH", "/usr/bin:/bin"),
        "AWS_LAMBDA_FUNCTION_NAME": binding.function_name,
        "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
        "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(binding.memory_mb),
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "LAMBDA_TASK_ROOT": binding.handler_path,
        "PYTHONUNBUFFERED": "1",
    }
    for name in binding.pass_env:
        if name in environ:
            env[name] = environ[name]
    return env


def invoke(binding: Binding, event: dict, context: dict, region: str) -> Outcome:
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fr-") as tmp:
        result_path = os.path.join(tmp, "result.json")
        job = {"module": binding.module, "function": binding.function,
               "handler_path": os.path.abspath(binding.handler_path), "event": event,
               "context": context, "result_path": result_path}
        proc = subprocess.Popen(
            [sys.executable, "-m", "bravo_function_runtime._child"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=child_env(binding, region) | {"PYTHONPATH": _package_root()}, text=True,
        )
        try:
            logs, _ = proc.communicate(json.dumps(job), timeout=binding.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            logs, _ = proc.communicate()
            return Outcome(Kind.TIMEOUT, _ms(start), f"Task timed out after {binding.timeout_s:g} seconds",
                           (logs or "")[-LOG_LIMIT:])
        try:
            with open(result_path, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, ValueError):
            return Outcome(Kind.CRASH, _ms(start), f"exited {proc.returncode} without a result",
                           (logs or "")[-LOG_LIMIT:])
    if result.get("ok"):
        return Outcome(Kind.OK, _ms(start), logs=(logs or "")[-LOG_LIMIT:])
    return Outcome(Kind.ERROR, _ms(start), f"{result.get('errorType')}: {result.get('errorMessage')}",
                   (logs or "")[-LOG_LIMIT:])


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
