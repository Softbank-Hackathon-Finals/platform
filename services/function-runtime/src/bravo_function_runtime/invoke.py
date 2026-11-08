"""부품 ②: 호출 관리. 핸들러를 하위 프로세스에서 부르고 제한 시간을 넘기면 프로세스를 끝낸다.

같은 프로세스 안의 호출은 제한 시간에 끊을 수 없어서 하위 프로세스를 쓴다(T46 spec D4).
호출마다 새 프로세스라 Lambda의 콜드 스타트와 같은 조건이다. 1단계 · 2단계 모두 이 부품을 그대로 쓴다.

- 하위 프로세스는 새 프로세스 그룹으로 띄우고, 끝나면(성공 · 실패 · 시간 초과 모두) 그룹 전체를 종료한다.
  핸들러가 띄운 손자 프로세스가 남아 다음 호출과 섞이지 않게 한다. Lambda도 반환 뒤 실행 환경을 멈춘다.
  `setsid`로 그룹을 스스로 벗어난 프로세스는 막지 못한다(컨테이너 단위 정리에 맡긴다).
- 작업 입력 · 결과 · 출력은 파일로 주고받는다. 파이프를 쓰지 않으므로 손자 프로세스가 출력 끝을 쥐고 있어도
  기다리지 않고, 출력은 끝 LOG_LIMIT바이트만 읽는다. 파일 크기 상한은 하위 프로세스가 스스로 건다(_child).
"""

from __future__ import annotations

import json
import os
import signal
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
    """Lambda가 넣어 주는 환경 변수와, 설정에서 넘기기로 한 값만 둔다.

    실행기 자신의 비밀(관리 토큰 · AWS 자격 · 큐 주소)은 설정 단계에서 pass_env에 넣을 수 없다(config.RESERVED_ENV).
    """
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
        job_path, result_path, log_path = (os.path.join(tmp, n) for n in ("job.json", "result.json", "out.log"))
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump({"module": binding.module, "function": binding.function,
                       "handler_path": os.path.abspath(binding.handler_path), "event": event,
                       "context": context, "result_path": result_path}, f)
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "bravo_function_runtime._child", job_path],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=child_env(binding, region) | {"PYTHONPATH": _package_root()}, start_new_session=True,
            )
            try:
                proc.wait(timeout=binding.timeout_s)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                _kill_group(proc.pid)
                proc.wait()
        logs = _tail(log_path)
        if timed_out:
            return Outcome(Kind.TIMEOUT, _ms(start), f"Task timed out after {binding.timeout_s:g} seconds", logs)
        try:
            with open(result_path, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, ValueError):
            return Outcome(Kind.CRASH, _ms(start), f"exited {proc.returncode} without a result", logs)
    if result.get("ok"):
        return Outcome(Kind.OK, _ms(start), logs=logs)
    return Outcome(Kind.ERROR, _ms(start), f"{result.get('errorType')}: {result.get('errorMessage')}", logs)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tail(path: str) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - LOG_LIMIT))
        return f.read().decode("utf-8", errors="replace")


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
