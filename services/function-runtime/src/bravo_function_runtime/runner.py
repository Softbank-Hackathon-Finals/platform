"""큐에서 꺼내 핸들러를 부르는 반복. Lambda의 SQS 이벤트 소스 매핑과 같은 규칙을 따른다.

- 핸들러가 성공하면 배치의 메시지를 모두 지운다.
- 실패 · 제한 시간 초과 · 비정상 종료면 지우지 않는다. 다시 꺼낼 수 있게 되는 시간이 지나면 다시 나오고,
  재시도 한도를 넘으면 큐의 실패 보관 큐 설정(RedrivePolicy)에 따라 큐 서버가 옮긴다. 실행기는 옮기지 않는다.
- 정지 스위치가 켜지면 새로 꺼내지 않고, 이미 꺼낸 배치만 끝낸다. 꺼내는 중인 요청도 처리 중으로 센다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import event as fmt
from .config import Binding, Config
from .invoke import Kind, Outcome, invoke
from .queue import Queue

log = logging.getLogger("bravo_function_runtime")
WAIT_S = 1  # 긴 대기 수신. 정지 스위치가 켜진 뒤 늦어도 이 시간 안에 새 수신이 멈춘다
BACKOFF_S = 1.0

Invoker = Callable[[Binding, dict, dict, str], Outcome]


@dataclass
class Counters:
    received: int = 0
    succeeded: int = 0
    failed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


class Runner:
    def __init__(self, config: Config, queues: dict[str, Queue], invoker: Invoker = invoke,
                 blocked: bool = False, clock: Callable[[], float] = time.time):
        self._config, self._queues, self._invoke, self._clock = config, queues, invoker, clock
        self._lock = threading.Lock()
        self._blocked = blocked
        self._in_flight = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.counters = Counters()
        self._arns = {name: q.arn() for name, q in queues.items()}

    # 정지 스위치 -----------------------------------------------------------
    def set_blocked(self, blocked: bool) -> dict:
        with self._lock:
            self._blocked = blocked
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {"write_blocked": self._blocked, "in_flight": self._in_flight}

    # 반복 ------------------------------------------------------------------
    def start(self) -> None:
        for binding in self._config.bindings:
            for n in range(binding.max_concurrency):
                t = threading.Thread(target=self._loop, args=(binding,), name=f"{binding.function_name}-{n}",
                                     daemon=True)
                t.start()
                self._threads.append(t)

    def stop(self, grace_s: float) -> bool:
        """새 수신을 멈추고 처리 중 배치가 끝나기를 기다린다. 다 끝났으면 True."""
        self.set_blocked(True)
        self._stop.set()
        deadline = time.monotonic() + grace_s
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return self.status()["in_flight"] == 0

    def _claim(self) -> bool:
        with self._lock:
            if self._blocked or self._stop.is_set():
                return False
            self._in_flight += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def _loop(self, binding: Binding) -> None:
        queue = self._queues[binding.queue_name]
        while not self._stop.is_set():
            if not self._claim():
                time.sleep(0.2)
                continue
            try:
                self.run_once(binding, queue)
            except Exception as e:  # noqa: BLE001 - 큐 서버 연결 오류로 반복을 끝내지 않는다
                log.warning("queue %s: %s", binding.queue_name, e)
                time.sleep(BACKOFF_S)
            finally:
                self._release()

    def run_once(self, binding: Binding, queue: Queue) -> Outcome | None:
        messages = queue.receive(binding.batch_size, WAIT_S)
        if not messages:
            return None
        deadline_ms = int((self._clock() + binding.timeout_s) * 1000)
        evt = fmt.event(messages, self._arns.get(binding.queue_name, ""), self._config.region)
        ctx = fmt.context(binding, self._config.region, self._config.account_id, deadline_ms)
        outcome = self._invoke(binding, evt, ctx, self._config.region)
        with self._lock:
            self.counters.received += len(messages)
            self.counters.by_kind[outcome.kind] = self.counters.by_kind.get(outcome.kind, 0) + 1
        if outcome.kind == Kind.OK:
            queue.delete(messages)
            with self._lock:
                self.counters.succeeded += len(messages)
        else:
            with self._lock:
                self.counters.failed += len(messages)
            log.warning("%s: %s %s (%d message(s) left for redelivery)", binding.function_name, outcome.kind,
                        outcome.error, len(messages))
        return outcome
