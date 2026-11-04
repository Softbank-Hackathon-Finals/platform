"""판정 입력인 창 요약(SliWindow)을 창별로 모아 낸다. 규칙은 docs/observability/README.md 4절이다.

창 내용(분류 · p95)은 T36 기준 구현 build_window가 만든다. 여기서는 창 경계만 정한다.
요청은 "응답을 받은 시각"이 속한 창에 센다. 보낸 시각으로 세면 시간 초과(15초)가 끝날 때까지
창을 닫지 못해 10초 판정을 맞출 수 없다.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from bravo_observability import Record, build_window


def rfc3339(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class WindowBuilder:
    def __init__(
        self, *, plan_id: str, env_pools: dict[str, str], window_seconds: int, start_wall: datetime
    ):
        self.plan_id = plan_id
        self.env_pools = env_pools
        self.window_seconds = window_seconds
        self.start_wall = start_wall
        self.late = 0
        self._next_index = 0
        self._open: dict[int, list[Record]] = defaultdict(list)

    def add(self, at: float, record: Record) -> None:
        """at은 부하 시작 기준 단조 시계 초다."""
        index = int(at // self.window_seconds)
        if index < self._next_index:
            self.late += 1
            return
        self._open[index].append(record)

    def close(self, index: int) -> dict:
        """창 하나를 닫는다. 비어 있어도 낸다. 0건이 보여야 판정이 UNKNOWN으로 멈춘다."""
        records = self._open.pop(index, [])
        self._next_index = max(self._next_index, index + 1)
        start = self.start_wall + timedelta(seconds=index * self.window_seconds)
        end = start + timedelta(seconds=self.window_seconds)
        return build_window(
            records,
            plan_id=self.plan_id,
            start=rfc3339(start),
            end=rfc3339(end),
            pools=self.env_pools,
        )

    def unemitted_requests(self) -> int:
        return sum(len(v) for v in self._open.values())


def check_continuity(windows: list[dict], *, window_seconds: int) -> list[dict]:
    """낸 창이 겹치지 않게 이어지는지 본다. 끊긴 곳을 채우지 않고 그대로 보고한다."""
    problems: list[dict] = []
    previous: dict | None = None
    for window in windows:
        start = datetime.fromisoformat(window["window"]["start"])
        end = datetime.fromisoformat(window["window"]["end"])
        if (end - start).total_seconds() != window_seconds:
            problems.append({"kind": "WRONG_LENGTH", "start": window["window"]["start"]})
        if previous is not None:
            if window["plan_id"] != previous["plan_id"]:
                problems.append({"kind": "PLAN_CHANGED", "start": window["window"]["start"]})
            previous_start = datetime.fromisoformat(previous["window"]["start"])
            previous_end = datetime.fromisoformat(previous["window"]["end"])
            if start == previous_start:
                problems.append({"kind": "DUPLICATE", "start": window["window"]["start"]})
            elif start < previous_end:
                problems.append({"kind": "OVERLAP", "start": window["window"]["start"]})
            elif start > previous_end:
                problems.append(
                    {
                        "kind": "GAP",
                        "after": previous["window"]["end"],
                        "next": window["window"]["start"],
                    }
                )
        previous = window
    return problems
