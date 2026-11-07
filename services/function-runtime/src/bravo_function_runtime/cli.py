"""`bravo-function-runtime check|run --config <파일>`.

check: 설정과 실제 큐 속성을 대조하고 지원하지 않는 항목을 모두 출력한다. 하나라도 있으면 종료 코드 2.
  --source <디렉터리>를 주면 함수마다 `<function_name>.mapping.json` · `<function_name>.function.json`
  (출발지 AWS 읽기 전용 API 출력)과도 대조한다(source.py). 파일이 없으면 그것도 거부 사유다.
run: check가 통과해야 시작한다. SIGTERM을 받으면 새 수신을 멈추고 처리 중 배치를 끝낸 뒤 종료한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading

from .admin import serve
from .config import Config, ConfigError, check_queue, load
from .queue import Queue, SqsQueue
from .runner import Runner
from .source import check_source

GRACE_S = 30


def _queues(config: Config) -> dict[str, Queue]:
    return {b.queue_name: SqsQueue(b.queue_name, config.region) for b in config.bindings}


def _problems(config: Config, queues: dict[str, Queue]) -> list[str]:
    out = []
    for b in config.bindings:
        out += check_queue(b, queues[b.queue_name].attributes())
    return out


def _source_problems(config: Config, directory: str) -> list[str]:
    out = []
    for b in config.bindings:
        try:
            with open(os.path.join(directory, f"{b.function_name}.mapping.json"), encoding="utf-8") as f:
                mapping = json.load(f)
            with open(os.path.join(directory, f"{b.function_name}.function.json"), encoding="utf-8") as f:
                function = json.load(f)
        except (OSError, ValueError) as e:
            out.append(f"{b.function_name}: 출발지 설정을 읽지 못했다({type(e).__name__})")
            continue
        out += check_source(b, mapping, function)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravo-function-runtime")
    p.add_argument("command", choices=["check", "run"])
    p.add_argument("--config", required=True)
    p.add_argument("--source", help="출발지 이벤트 소스 매핑 · 함수 설정 JSON 디렉터리")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load(a.config)
    except ConfigError as e:
        for problem in e.problems:
            print(f"unsupported: {problem}", file=sys.stderr)
        return 2
    problems = _source_problems(config, a.source) if a.source else []
    queues = _queues(config)
    problems += _problems(config, queues)
    for problem in problems:
        print(f"unsupported: {problem}", file=sys.stderr)
    if problems:
        return 2
    if a.command == "check":
        print("ok")
        return 0
    runner = Runner(config, queues, blocked=os.environ.get("WRITE_BLOCKED", "false") == "true")
    server = serve(runner, config.admin_port, os.environ.get("ADMIN_TOKEN"))
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    runner.start()
    done.wait()
    drained = runner.stop(GRACE_S)
    server.shutdown()
    return 0 if drained else 1


if __name__ == "__main__":
    sys.exit(main())
