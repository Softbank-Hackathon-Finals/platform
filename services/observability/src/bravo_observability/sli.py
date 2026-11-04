"""창 요약(SliWindow) 생성과 SHIFT · WATCH 판정(SliJudgement). 규칙은 docs/observability/README.md 3 · 4절.

LLM · 네트워크 · 시계를 쓰지 않는다. 같은 입력이면 같은 결과다(NFR-5).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Pool = Literal["source", "target"]
State = Literal["SHIFT", "WATCH"]


@dataclass(frozen=True)
class Record:
    """부하 생성기가 Edge로 보낸 요청 하나의 결과.

    env: 응답 헤더 X-Bravo-Env 값(없으면 None). status: HTTP 상태(응답이 없으면 None).
    failure: 응답을 받지 못한 이유. 있으면 status와 env는 보지 않는다.
    """

    env: str | None
    status: int | None
    latency_ms: float | None
    failure: Literal["timeout", "connect"] | None = None


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _p95(latencies: list[float]) -> float | None:
    """nearest-rank: 오름차순에서 ceil(0.95 × n)번째 값."""
    if not latencies:
        return None
    ordered = sorted(latencies)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def build_window(
    records: Iterable[Record], *, plan_id: str, start: str, end: str, pools: Mapping[str, Pool]
) -> dict:
    """한 창의 요청 결과를 SliWindow로 요약한다.

    pools: X-Bravo-Env 값 → 환경 역할. 계획에 등록한 환경만 넣는다. 없는 값은 환경 미확인 오류다.
    received_at은 창 끝으로 채우고, 플랫폼이 받을 때 receive()로 덮어쓴다.
    """
    stats = {p: {"requests": 0, "http_5xx": 0, "integrity_conflict": 0} for p in ("source", "target")}
    latencies: dict[str, list[float]] = {"source": [], "target": []}
    unattributed = {"timeout": 0, "connect": 0, "other": 0}
    for r in records:
        if r.failure is not None:
            unattributed[r.failure] += 1
            continue
        pool = pools.get(r.env) if r.env is not None else None
        if pool is None:
            unattributed["other"] += 1
            continue
        s = stats[pool]
        s["requests"] += 1
        if r.status is not None and 500 <= r.status <= 599:
            s["http_5xx"] += 1
        elif r.status == 409:
            s["integrity_conflict"] += 1
        if r.latency_ms is not None:
            latencies[pool].append(r.latency_ms)
    return {
        "kind": "sli_window",
        "schema_version": "1",
        "plan_id": plan_id,
        "source": "loadgen",
        "window": {"start": start, "end": end},
        "received_at": end,
        "pools": {p: {**stats[p], "p95_ms": _p95(latencies[p])} for p in ("source", "target")},
        "unattributed": unattributed,
    }


def receive(window: dict, now: str) -> dict:
    """플랫폼이 창을 받은 시각으로 received_at을 덮어쓴다. 생산자 값은 버린다."""
    return {**window, "received_at": now}


def _usable(window: dict, plan_id: str, observe_from: datetime, now: datetime, p: dict) -> set[str]:
    reasons = set()
    start, end = _ts(window["window"]["start"]), _ts(window["window"]["end"])
    received = _ts(window["received_at"])
    if window["plan_id"] != plan_id:
        reasons.add("PLAN_MISMATCH")
    if start < observe_from:
        reasons.add("WINDOW_BEFORE_CHANGE")
    if (end - start).total_seconds() != p["window_seconds"]:
        reasons.add("WINDOW_MISMATCH")
    if (end - received).total_seconds() > p["max_clock_skew_seconds"] or received > now:
        reasons.add("CLOCK_SKEW")
    if (now - received).total_seconds() > p["stale_after_seconds"] or (
        now - end
    ).total_seconds() > p["stale_after_seconds"] + p["max_clock_skew_seconds"]:
        reasons.add("STALE")
    return reasons


def _content(window: dict, weight_target: int, p: dict) -> tuple[set[str], set[str]]:
    fail, unknown = set(), set()
    src, tgt = window["pools"]["source"], window["pools"]["target"]
    unattributed = sum(window["unattributed"].values())
    total = src["requests"] + tgt["requests"] + unattributed
    errors = src["http_5xx"] + tgt["http_5xx"] + unattributed
    w = weight_target / 100
    if src["integrity_conflict"] + tgt["integrity_conflict"] > 0:
        fail.add("INTEGRITY_CONFLICT")
    if errors / max(total, p["min_requests"]) > p["error_rate_max"]:
        fail.add("ERROR_RATE")
    if total < p["min_requests"]:
        unknown.add("INSUFFICIENT_SAMPLES")
    for pool, share in ((src, 1 - w), (tgt, w)):
        if share == 0:
            continue
        if pool["http_5xx"] / max(pool["requests"], p["min_pool_requests"]) > p["error_rate_max"]:
            fail.add("ERROR_RATE")
        if pool["requests"] < p["min_pool_requests"]:
            unknown.add("INSUFFICIENT_SAMPLES")
        elif pool["p95_ms"] > p["p95_ms_max"]:
            fail.add("P95")
    attributed = src["requests"] + tgt["requests"]
    if attributed == 0 or abs(tgt["requests"] / attributed - w) > p["share_tolerance"]:
        unknown.add("NOT_APPLIED")
    return fail, unknown


def _action(verdict: str, state: State, prior_unknown: int, p: dict) -> str:
    if state == "SHIFT":
        if verdict == "PASS":
            return "ADVANCE_ALLOWED"
        if verdict == "FAIL" or prior_unknown + 1 >= p["unknown_limit"]:
            return "SET_WEIGHT_0"
        return "HOLD"
    return {"PASS": "NONE", "FAIL": "ALERT_FAILBACK_PROPOSAL"}.get(verdict, "ALERT_UNOBSERVABLE")


def judge(
    profile: dict,
    window: dict | None,
    *,
    plan_id: str,
    state: State,
    weight_target: int,
    observe_from: str,
    decided_at: str,
    prior_unknown: int,
) -> dict:
    """창 하나로 판정한다. plan_id · state · weight_target · observe_from은 서버 상태에서 가져온다.

    prior_unknown은 같은 observe_from 안에서 직전까지 연속된 UNKNOWN 수다.
    """
    fail: set[str] = set()
    unknown = (
        {"MISSING"}
        if window is None
        else _usable(window, plan_id, _ts(observe_from), _ts(decided_at), profile)
    )
    if window is not None and not unknown:
        fail, unknown = _content(window, weight_target, profile)
    verdict = "FAIL" if fail else "UNKNOWN" if unknown else "PASS"
    return {
        "kind": "sli_judgement",
        "schema_version": "1",
        "plan_id": plan_id,
        "state": state,
        "profile": {"id": profile["id"], "version": profile["version"]},
        "weight_target": weight_target,
        "observe_from": observe_from,
        "decided_at": decided_at,
        "prior_unknown": prior_unknown,
        "window": window,
        "verdict": verdict,
        "reasons": sorted(fail | unknown),
        "action": _action(verdict, state, prior_unknown, profile),
    }
