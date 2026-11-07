"""`bravo-function-runtime check|run --config <파일>`.

check: 설정과 실제 큐 속성을 대조하고 지원하지 않는 항목을 모두 출력한다. 하나라도 있으면 종료 코드 2.
run: check가 통과해야 시작한다. SIGTERM을 받으면 새 수신을 멈추고 처리 중 배치를 끝낸 뒤 종료한다.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .admin import serve
from .config import Config, ConfigError, check_queue, load
from .queue import Queue, SqsQueue
from .runner import Runner

GRACE_S = 30


def _queues(config: Config) -> dict[str, Queue]:
    return {b.queue_name: SqsQueue(b.queue_name, config.region) for b in config.bindings}


def _problems(config: Config, queues: dict[str, Queue]) -> list[str]:
    out = []
    for b in config.bindings:
        out += check_queue(b, queues[b.queue_name].attributes())
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bravo-function-runtime")
    p.add_argument("command", choices=["check", "run"])
    p.add_argument("--config", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load(a.config)
    except ConfigError as e:
        for problem in e.problems:
            print(f"unsupported: {problem}", file=sys.stderr)
        return 2
    queues = _queues(config)
    problems = _problems(config, queues)
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
