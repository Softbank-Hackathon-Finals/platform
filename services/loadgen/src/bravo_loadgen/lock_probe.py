"""메시지 쓰기 잠금의 대기 시간을 잰다.

demo-chat은 커밋 순서를 id 순서와 맞추려고 모든 메시지 쓰기를 advisory lock 하나로 줄 세운다
(app/store.py MESSAGE_WRITE_LOCK). 같은 잠금을 짧게 잡아 보면 새 쓰기가 줄에서 얼마나 기다리는지 나온다.
앱이 잡는 잠금이므로 앱 안에서 재지 않고 DB에서 직접 잰다.

스레드로 돈다. 멈춘 DB에 걸려도 부하를 막지 않고, 끝날 때 그 스레드를 버리고 나갈 수 있어야 한다.
자동 커밋으로 잡아 문장이 끝나면 잠금이 바로 풀린다. 결과를 받고 놓으면 측정기 쪽 왕복만큼 앱 쓰기를 막는다.
"""

import threading
import time
from dataclasses import dataclass

import psycopg

from bravo_loadgen.stats import nearest_rank

MESSAGE_WRITE_LOCK = 4_310_001  # demo-chat app/store.py와 같은 값 (testing/e2e.py가 실제 값과 맞춰 본다)
PROBE_INTERVAL_S = 0.2
PROBE_STATEMENT_TIMEOUT_MS = 5000
STOP_WAIT_S = PROBE_STATEMENT_TIMEOUT_MS / 1000 + 2
# 클라이언트가 이보다 오래 기다리면 잠금과 상관없이 DB나 경로가 멈춘 것으로 본다.
STALL_MS = 1000

# 대상 목록은 왼쪽부터 평가된다. 앞뒤 시각을 DB 시계로 재서 네트워크 왕복을 뺀다.
_WAIT_SQL = "SELECT clock_timestamp(), pg_advisory_xact_lock(%(key)s::bigint), clock_timestamp()"
_WAITERS_SQL = """
SELECT count(*) FROM pg_locks
WHERE locktype = 'advisory' AND classid = 0 AND objid = %(key)s AND objsubid = 1 AND NOT granted
"""


@dataclass(frozen=True)
class LockSample:
    phase: str
    wait_ms: float  # DB 시계로 잰 잠금 대기
    waiting_sessions: int
    timed_out: bool
    elapsed_ms: float  # 클라이언트가 기다린 시간. DB가 통째로 멈추면 이쪽에만 드러난다


class LockProbe:
    """연결 문자열은 비밀이다. 기록하지 않고 예외 메시지도 내보내지 않는다."""

    def __init__(self, dsn: str, key: int = MESSAGE_WRITE_LOCK):
        self._dsn = dsn
        self.key = key
        self.phase = "baseline"
        self.samples: list[LockSample] = []
        self.connection_errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._in_flight: tuple[str, float] | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(STOP_WAIT_S)
        in_flight = self._in_flight
        if self._thread.is_alive() and in_flight is not None:
            # 끝날 때까지 답이 없었다. 버리면 멈춘 구간이 값에서 사라진다.
            phase, started = in_flight
            elapsed_ms = (time.monotonic() - started) * 1000
            self.samples.append(LockSample(phase, elapsed_ms, 0, True, elapsed_ms))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._connect() as connection:
                    connection.execute(
                        f"SET statement_timeout = {PROBE_STATEMENT_TIMEOUT_MS}"  # 정수 상수
                    )
                    while not self._stop.is_set():
                        self.samples.append(self._sample(connection))
                        self._stop.wait(PROBE_INTERVAL_S)
            except psycopg.Error:
                # 컷오버 중 DB가 멈추거나 바뀔 수 있다. 부하를 멈추지 않고 다시 붙는다.
                self._in_flight = None
                self.connection_errors += 1
                self._stop.wait(PROBE_INTERVAL_S)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            self._dsn, autocommit=True, prepare_threshold=None, connect_timeout=5
        )

    def _sample(self, connection: psycopg.Connection) -> LockSample:
        phase = self.phase
        started = time.monotonic()
        self._in_flight = (phase, started)
        waiters = connection.execute(_WAITERS_SQL, {"key": self.key}).fetchone()[0]
        try:
            before, _, after = connection.execute(_WAIT_SQL, {"key": self.key}).fetchone()
            wait_ms = (after - before).total_seconds() * 1000
            timed_out = False
        except psycopg.errors.QueryCanceled:
            wait_ms = float(PROBE_STATEMENT_TIMEOUT_MS)
            timed_out = True
        self._in_flight = None
        return LockSample(phase, wait_ms, waiters, timed_out, (time.monotonic() - started) * 1000)


def _waits_including_delayed_probes(samples: list[LockSample]) -> list[float]:
    """표본은 앞 표본이 끝나야 잡힌다. 오래 막히면 표본이 하나뿐이라 백분위에 안 보인다.

    일정 간격으로 왔을 요청들이 봤을 대기까지 넣어 부하 쪽 open-loop와 같은 기준으로 맞춘다.
    """
    interval_ms = PROBE_INTERVAL_S * 1000
    waits: list[float] = []
    for sample in samples:
        delayed = int(sample.wait_ms // interval_ms)
        waits += [sample.wait_ms - k * interval_ms for k in range(delayed + 1)]
    return waits


def _phase_summary(samples: list[LockSample]) -> dict:
    if not samples:
        return {
            "samples": 0,
            "wait_ms": None,
            "round_trip_ms": None,
            "waiting_sessions_max": None,
            "timed_out": 0,
            "stalled": 0,
        }
    waits = _waits_including_delayed_probes(samples)
    elapsed = [s.elapsed_ms for s in samples]
    return {
        "samples": len(samples),
        "wait_ms": {
            "p50": round(nearest_rank(waits, 0.50), 3),
            "p95": round(nearest_rank(waits, 0.95), 3),
            "p99": round(nearest_rank(waits, 0.99), 3),
            "max": round(max(waits), 3),
        },
        "round_trip_ms": {"p95": round(nearest_rank(elapsed), 3), "max": round(max(elapsed), 3)},
        "waiting_sessions_max": max(s.waiting_sessions for s in samples),
        "timed_out": sum(s.timed_out for s in samples),
        "stalled": sum(s.elapsed_ms > STALL_MS for s in samples),
    }


def summarize_lock_samples(
    samples: list[LockSample], *, key: int, connection_errors: int = 0
) -> dict:
    return {
        "key": key,
        "interval_s": PROBE_INTERVAL_S,
        "connection_errors": connection_errors,
        "baseline": _phase_summary([s for s in samples if s.phase == "baseline"]),
        "load": _phase_summary([s for s in samples if s.phase == "load"]),
    }
