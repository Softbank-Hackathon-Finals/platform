"""쓰기 · 읽기 부하를 정해진 속도로 보낸다.

응답을 기다리지 않고 예정 시각마다 보낸다(open-loop). 응답을 기다려 다음 요청을 보내면
서버가 멈춘 동안 요청 자체가 줄어 장애가 숫자에서 사라진다.
"""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from bravo_observability import Record

from bravo_loadgen.journal import Journal, message_author, message_body
from bravo_loadgen.schema import SchemaViolation, validate_window
from bravo_loadgen.windows import WindowBuilder, check_continuity

MESSAGES_PATH = "/api/messages"
ENV_HEADER = "X-Bravo-Env"

WindowSink = Callable[[dict], Awaitable[None]]
# 로그인을 켠 앱(T47): 쓰기 시도마다 그때의 토큰 헤더를 받는다. 재시도도 새로 받은 토큰을 싣는다.
WriteHeaders = Callable[[], Mapping[str, str]]
# 로그인 때문에 거부된 쓰기. 창은 5xx · 409만 오류로 세므로, 이 뒤의 창을 내면 쓰기가 막힌 동안 정상으로 보인다.
AUTH_REJECTED = (401, 403)


@dataclass(frozen=True)
class LoadConfig:
    run_id: str
    plan_id: str
    env_pools: dict[str, str]
    duration_s: float
    write_rps: float = 50  # 설계 5.3
    read_rps: float = 150  # 설계 5.3
    window_seconds: int = 10  # 기준 프로필 demo-shift
    attempt_timeout_s: float = 15  # 설계 5.3 클라이언트 timeout
    # 재시도 한도는 브라우저 클라이언트(demo-chat transport.js)와 같다.
    retry_budget_s: float = 60
    max_attempts: int = 32
    # 로그인을 켠 앱은 작성자가 토큰 이름이어야 한다(T47). 없으면 실행마다 다른 loadgen-<run_id>.
    author: str | None = None

    def __post_init__(self):
        # author는 40자 제한이고 "loadgen-" 8자를 쓴다.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", self.run_id):
            raise ValueError("run_id must match [a-z0-9-] and be at most 32 characters")
        if self.author is not None and not 1 <= len(self.author) <= 40:
            raise ValueError("author must be 1 to 40 characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.plan_id):
            raise ValueError("plan_id does not match the SliWindow Identifier pattern")
        if sorted(self.env_pools.values()) != ["source", "target"]:
            raise ValueError("env_pools must map one environment to source and one to target")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")
        if self.duration_s <= 0 or self.write_rps < 0 or self.read_rps < 0:
            raise ValueError("duration_s must be positive and rates must not be negative")


@dataclass
class RunResult:
    start_wall: datetime
    scheduled_writes: int
    scheduled_reads: int
    windows: list[dict]
    tail_requests: int
    late_requests: int
    delivery_failed: int
    invalid_windows: int
    continuity_problems: list[dict]
    withheld_windows: int = 0
    withheld_requests: int = 0


def retry_delay(header: str | None, attempt_index: int) -> float:
    """demo-chat app/static/transport.js의 retryDelay와 같은 규칙(초 단위)."""
    try:
        seconds = float(header) if header else 0.0
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        return min(0.5 * 2 ** min(attempt_index, 4), 5.0)
    return max(0.5, min(seconds, 30.0))


class LoadRunner:
    def __init__(
        self,
        config: LoadConfig,
        client: httpx.AsyncClient,
        journal: Journal,
        window_sink: WindowSink | None = None,
        write_headers: WriteHeaders | None = None,
    ):
        self.config = config
        self.client = client
        self.journal = journal
        self.window_sink = window_sink
        self.write_headers = write_headers
        self.author = config.author or message_author(config.run_id)
        self._last_acked_id: int | None = None
        self._builder: WindowBuilder
        self._t0 = 0.0

    def _now(self) -> float:
        return asyncio.get_running_loop().time() - self._t0

    async def run(self) -> RunResult:
        config = self.config
        start_wall = datetime.now(UTC)
        self._t0 = asyncio.get_running_loop().time()
        self._builder = WindowBuilder(
            plan_id=config.plan_id,
            env_pools=config.env_pools,
            window_seconds=config.window_seconds,
            start_wall=start_wall,
        )
        self.journal.write(
            {
                "type": "run_start",
                "run_id": config.run_id,
                "plan_id": config.plan_id,
                "author": self.author,
                "start_wall": start_wall.isoformat(),
                "duration_s": config.duration_s,
                "write_rps": config.write_rps,
                "read_rps": config.read_rps,
                "window_seconds": config.window_seconds,
                "attempt_timeout_s": config.attempt_timeout_s,
                "retry_budget_s": config.retry_budget_s,
                "env_pools": config.env_pools,
            }
        )
        self._windows: list[dict] = []
        self._delivery_failed = 0
        self._invalid_windows = 0
        self._auth_rejected = False
        self._withheld_windows = 0
        self._withheld_requests = 0
        stop = asyncio.Event()
        emitter = asyncio.create_task(self._emit_windows(stop))

        writes = round(config.write_rps * config.duration_s)
        reads = round(config.read_rps * config.duration_s)
        async with asyncio.TaskGroup() as group:
            group.create_task(self._pace(group, config.write_rps, writes, self._write))
            group.create_task(self._pace(group, config.read_rps, reads, self._read))
        ended_at = self._now()
        # 마지막 요청은 경계 직전에 끝난다. 부하 시간을 다 채워야 마지막 창을 닫을 수 있다.
        if ended_at < config.duration_s:
            await asyncio.sleep(config.duration_s - ended_at)

        stop.set()
        await emitter
        self.journal.write(
            {
                "type": "run_end",
                "ended_at_s": round(ended_at, 6),
                "scheduled_writes": writes,
                "scheduled_reads": reads,
            }
        )
        self.journal.flush()
        return RunResult(
            start_wall=start_wall,
            scheduled_writes=writes,
            scheduled_reads=reads,
            windows=self._windows,
            tail_requests=self._builder.unemitted_requests(),
            late_requests=self._builder.late,
            delivery_failed=self._delivery_failed,
            invalid_windows=self._invalid_windows,
            continuity_problems=check_continuity(
                self._windows, window_seconds=config.window_seconds
            ),
            withheld_windows=self._withheld_windows,
            withheld_requests=self._withheld_requests,
        )

    async def _pace(self, group: asyncio.TaskGroup, rate: float, total: int, send) -> None:
        for index in range(total):
            due = index / rate
            delay = due - self._now()
            if delay > 0:
                await asyncio.sleep(delay)
            group.create_task(send(due))

    async def _write(self, due: float) -> None:
        config = self.config
        logical_id = str(uuid.uuid4())
        payload = {
            "idempotency_key": logical_id,
            "author": self.author,
            "body": message_body(config.run_id, logical_id),
        }
        deadline = self._now() + config.retry_budget_s
        attempt_no = 0
        while True:
            attempt_no += 1
            record, retry_after = await self._attempt(
                "write",
                logical_id,
                attempt_no,
                due,
                min(config.attempt_timeout_s, max(deadline - self._now(), 0.001)),
                "POST",
                json=payload,
                headers=self.write_headers() if self.write_headers else None,
            )
            if record["acked"]:
                self._last_acked_id = record["message_id"]
            if record["status"] in AUTH_REJECTED:
                self._auth_rejected = True
            status = record["status"]
            final = record["acked"] or (status is not None and 400 <= status < 500 and status != 429)
            delay = 0.0
            if not final:
                delay = retry_delay(retry_after, attempt_no - 1)
                final = attempt_no >= config.max_attempts or delay >= deadline - self._now()
            record["final"] = final
            self.journal.write(record)
            if final:
                return
            await asyncio.sleep(delay)

    async def _read(self, due: float) -> None:
        params = {} if self._last_acked_id is None else {"after": self._last_acked_id}
        record, _ = await self._attempt(
            "read",
            str(uuid.uuid4()),
            1,
            due,
            self.config.attempt_timeout_s,
            "GET",
            params=params,
        )
        record["final"] = True
        self.journal.write(record)

    async def _attempt(
        self,
        kind: str,
        logical_id: str,
        attempt_no: int,
        due: float,
        timeout: float,
        method: str,
        **request,
    ) -> tuple[dict, str | None]:
        sent_at = self._now()
        status = env = message_id = retry_after = None
        acked = False
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.request(method, MESSAGES_PATH, **request)
            outcome = "http"
            status = response.status_code
            env = response.headers.get(ENV_HEADER)
            retry_after = response.headers.get("Retry-After")
            if kind == "write":
                message_id = _message_id(response)
                acked = status in (200, 201) and message_id is not None
            else:
                acked = status == 200
        except TimeoutError:
            outcome = "timeout"
        except httpx.RequestError:
            # 연결 실패뿐 아니라 깨진 응답(DecodingError)도 응답을 받지 못한 것으로 센다.
            outcome = "connect"
        finished_at = self._now()
        latency_ms = (finished_at - sent_at) * 1000
        # 재는 시각과 세는 시각 사이에 await가 없어 이미 닫힌 창으로 들어가지 않는다.
        failure = None if outcome == "http" else outcome
        self._builder.add(finished_at, Record(env, status, latency_ms, failure))
        record = {
            "type": "attempt",
            "kind": kind,
            "logical_id": logical_id,
            "attempt_id": f"{logical_id}.{attempt_no}",
            "attempt_no": attempt_no,
            "lag_ms": round((sent_at - due) * 1000, 3) if attempt_no == 1 else None,
            "sent_at_s": round(sent_at, 6),
            "finished_at_s": round(finished_at, 6),
            "latency_ms": round(latency_ms, 3),
            "outcome": outcome,
            "status": status,
            "env": env,
            "message_id": message_id,
            "acked": acked,
        }
        return record, retry_after

    async def _emit_windows(self, stop: asyncio.Event) -> None:
        """창 경계마다 닫는다. 끝나지 않은 마지막 창은 길이가 달라 내지 않고 따로 센다."""
        index = 0
        while True:
            remaining = (index + 1) * self.config.window_seconds - self._now()
            if remaining > 0:
                if stop.is_set():
                    return
                try:
                    await asyncio.wait_for(stop.wait(), timeout=remaining)
                    continue
                except TimeoutError:
                    pass
            await self._publish(self._builder.close(index))
            self.journal.flush()
            index += 1

    async def _publish(self, window: dict) -> None:
        if self._auth_rejected:
            # 받는 쪽은 창이 없으면 UNKNOWN으로 멈춘다(계약 3절). 부하와 대조는 그대로 하고 리포트에 남긴다.
            self._withheld_windows += 1
            self._withheld_requests += sum(window["pools"][p]["requests"] for p in ("source", "target"))
            self._withheld_requests += sum(window["unattributed"].values())
            return
        try:
            validate_window(window)
        except SchemaViolation:
            # 계약을 어긴 창은 내지 않는다. 받는 쪽은 창이 없으면 UNKNOWN으로 멈춘다.
            self._invalid_windows += 1
            return
        self._windows.append(window)
        if self.window_sink is None:
            return
        try:
            await self.window_sink(window)
        except httpx.HTTPError:
            # 다시 보내지 않는다. 늦은 창은 판정에 쓰이지 않고, 없는 창은 UNKNOWN이다(계약 3절).
            self._delivery_failed += 1


def _message_id(response: httpx.Response) -> int | None:
    try:
        message_id = response.json().get("id")
    except (ValueError, AttributeError):
        return None
    return message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None
