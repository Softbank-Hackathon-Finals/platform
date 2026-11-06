"""SHIFT 단계가 정상 트래픽에서 스스로 끝나지 않는지, 관측이 끊기면 끝나는지 호스트 방식으로 돌려 본다.

edge-control 호스트처럼 1초마다 가장 최근에 받은 창을 판정하고, 같은 창이라도 서버 시각 10초 구간이
바뀌면 새 관측으로 본다. 연속 수는 next_prior_unknown으로 구한다. 확인 시각 · 창 위상 · 전송 지연을
바꿔 가며 계약 문서 3절의 근거(J25 · J26)를 재현한다.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bravo_observability import judge, next_prior_unknown

DOCS = Path(__file__).resolve().parents[3] / "docs/observability"
PROFILE = next(p for p in json.loads((DOCS / "threshold-profiles.json").read_text())["profiles"]
               if p["id"] == "demo-shift")
BASE = datetime(2026, 11, 8, 5, 0, 0, tzinfo=UTC)
RPS, OLD, NEW = 200, 0, 10
GRID = [(phase / 2, 100 + confirm / 2, delay)
        for delay in (0.5, 1, 2, 3) for phase in range(20) for confirm in range(20)]


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def window(start, delay, weight):
    total = RPS * 10
    target = total * weight // 100
    return {"kind": "sli_window", "schema_version": "1", "plan_id": "plan-demo", "source": "loadgen",
            "window": {"start": at(start), "end": at(start + 10)}, "received_at": at(start + 10 + delay),
            "pools": {"source": {"requests": total - target, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": 120},
                      "target": {"requests": target, "http_5xx": 0, "integrity_conflict": 0,
                                 "p95_ms": 260 if target else None}},
            "unattributed": {"timeout": 0, "connect": 0, "other": 0}}


def step(phase, confirm, delay, *, edge_lag=0, windows_stop=False, applied=True):
    """Return (action, seconds after confirm) of the first PASS or SET_WEIGHT_0."""
    previous, observation = None, None
    t = confirm
    while t < confirm + 120:
        index = int((t - phase - delay) // 10) - 1  # last window whose end + delay has passed
        if windows_stop:
            index = min(index, int((confirm - phase) // 10) - 1)
        start = phase + 10 * index
        weight = NEW if applied and start >= confirm + edge_lag else OLD
        current = (start, int((BASE + timedelta(seconds=t)).timestamp()) // PROFILE["window_seconds"])
        prior = next_prior_unknown(previous, same_observation=current == observation)
        decision = judge(PROFILE, window(start, delay, weight), plan_id="plan-demo", state="SHIFT",
                         weight_target=NEW, observe_from=at(confirm), decided_at=at(t), prior_unknown=prior)
        if decision["action"] in ("ADVANCE_ALLOWED", "SET_WEIGHT_0"):
            return decision["action"], t - confirm
        previous, observation = decision, current
        t += 1
    return "HOLD", t - confirm


@pytest.mark.parametrize("edge_lag", [0, 5])
def test_healthy_step_is_never_ended_by_the_wait_for_the_first_window(edge_lag):
    results = [step(*case, edge_lag=edge_lag) for case in GRID]
    assert {action for action, _ in results} == {"ADVANCE_ALLOWED"}
    assert max(seconds for _, seconds in results) <= 30


@pytest.mark.parametrize("kind", ["windows_stop", "not_applied"])
def test_lost_observation_or_unapplied_weight_still_ends_the_step(kind):
    options = {"windows_stop": True} if kind == "windows_stop" else {"applied": False}
    results = [step(*case, **options) for case in GRID]
    assert {action for action, _ in results} == {"SET_WEIGHT_0"}
    assert max(seconds for _, seconds in results) <= 45


def test_next_prior_unknown():
    unknown = {"verdict": "UNKNOWN", "prior_unknown": 1, "reasons": ["MISSING"]}
    before = {"verdict": "UNKNOWN", "prior_unknown": 1, "reasons": ["WINDOW_BEFORE_CHANGE"]}
    aged = {"verdict": "UNKNOWN", "prior_unknown": 1, "reasons": ["STALE", "WINDOW_BEFORE_CHANGE"]}
    assert next_prior_unknown(None, same_observation=False) == 0
    assert next_prior_unknown(unknown, same_observation=True) == 1
    assert next_prior_unknown(unknown, same_observation=False) == 2
    assert next_prior_unknown(before, same_observation=False) == 1
    assert next_prior_unknown(aged, same_observation=False) == 2
    for verdict in ("PASS", "FAIL"):
        assert next_prior_unknown({"verdict": verdict, "prior_unknown": 4, "reasons": []},
                                  same_observation=False) == 0
