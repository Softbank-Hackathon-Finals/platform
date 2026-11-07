"""기준 프로필 실측 확정: T18 `align` 결과(weight-alignment.json)에서 p95 상한과 UNKNOWN 한도를 제안한다.

T36 할 일 4의 계산만 한다. 프로필 파일은 쓰지 않는다. 사람이 제안값과 근거를 보고
threshold-profiles.json에 새 버전으로 올린다.

    python -m bravo_observability.calibrate out/<실행 ID>/weight-alignment.json [threshold-profiles.json]

- p95 상한: judge가 p95를 보는 창 · 풀(가중치가 있는 환경, 풀 요청이 min_pool_requests 이상)의 p95
  최대값 × 1.5를 100ms 단위로 올린다. 표본이 적은 창의 p95는 판정도 보지 않으므로 넣지 않는다.
- UNKNOWN 한도: Edge 확인 뒤에도 새 가중치가 보이지 않는 창(NOT_APPLIED · 표본 부족)이 가장 길게
  이어진 수 + 2(전송 한 번 늦음 · 판정 구간 경계 여유), 최소 3. 변경 전에 시작한 창은 연속 수에
  넣지 않으므로(J25) 확인 뒤의 반영 지연만 센다.
- Edge를 거치지 않은 측정, 경고가 있는 실행, 판정에 쓸 수 없는 실행, 관측하지 못한 단계, FAIL 창이
  있으면 값을 내지 않는다. 그런 실행으로 기준을 정하면 실제 데모와 다른 조건이 기준이 된다.
"""

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

P95_MARGIN = 1.5
P95_STEP_MS = 100
UNKNOWN_MARGIN = 2
UNKNOWN_FLOOR = 3
LAG_REASONS = {"NOT_APPLIED", "INSUFFICIENT_SAMPLES"}
PROFILES = Path(__file__).resolve().parents[4] / "docs/observability/threshold-profiles.json"  # 레포에서 실행할 때


class CalibrationRefused(ValueError):
    """이 실행으로는 기준을 정할 수 없다. args[0]은 사유 목록이다."""


def _refusals(alignment: dict) -> list[str]:
    reasons = []
    measurement = alignment.get("measurement", {})
    if measurement.get("path") != "edge" or measurement.get("edge_measured") is not True:
        reasons.append("NOT_EDGE_MEASURED")
    if alignment.get("warnings"):
        reasons.append("RUN_HAS_WARNINGS")
    if alignment.get("sli_usable_for_judgement") is not True:
        reasons.append("SLI_NOT_USABLE")
    if not alignment.get("complete"):
        reasons.append("STEP_NOT_OBSERVED")
    steps = alignment.get("steps", [])
    if not {10, 50, 100} <= {step["weight_target"] for step in steps}:
        reasons.append("STEPS_MISSING")
    if any(step["receipt_status"] == "CONFIRMED" and step["provider_confirmed_after_s"] is None for step in steps):
        reasons.append("CONFIRMATION_TIME_MISSING")
    if any(row["reference_verdict"] == "FAIL" for step in steps for row in step["windows"]):
        reasons.append("RUN_HAS_FAIL_WINDOW")  # 정상 실행이 아니다(오류율 · p95 등)
    return reasons


def _p95_samples(step: dict, min_pool_requests: int) -> list[int]:
    """judge가 p95를 보는 창 · 풀의 p95만: 가중치가 있는 환경(J16)이고 풀 요청이 min_pool_requests 이상.

    반영 지연 창(NOT_APPLIED)도 judge는 표본이 충분한 풀의 p95를 보므로 뺄 수 없다.
    """
    pools = (["source"] if step["weight_target"] < 100 else []) + (["target"] if step["weight_target"] > 0 else [])
    return [row[pool]["p95_ms"] for row in step["windows"] for pool in pools
            if row[pool]["requests"] >= min_pool_requests and row[pool]["p95_ms"] is not None]


def _next_version(profile: dict, versions) -> str:
    """같은 id의 가장 새 버전 + 1. 이전 버전 실행을 다시 넣어도 이미 있는 버전을 내지 않는다."""
    try:
        return str(max(int(v) for v in (profile["version"], *versions)) + 1)
    except ValueError:
        raise CalibrationRefused(["PROFILE_VERSION_NOT_NUMERIC"]) from None


def _lag_windows(step: dict) -> int:
    """Edge 확인 시각 뒤에 시작해 아직 새 가중치를 보이지 않은 창이 앞에서부터 몇 개 이어졌나."""
    confirmed = datetime.fromisoformat(step["request_at"]) + timedelta(seconds=step["provider_confirmed_after_s"])
    count = 0
    for row in step["windows"]:
        if datetime.fromisoformat(row["start"]) < confirmed:
            continue
        if not LAG_REASONS & set(row["reasons"]):
            break
        count += 1
    return count


def calibrate(alignment: dict, profile: dict, *, versions=()) -> dict:
    """versions: threshold-profiles.json에 같은 id로 이미 있는 버전들."""
    refusals = _refusals(alignment)
    if refusals:
        raise CalibrationRefused(refusals)
    steps = [step for step in alignment["steps"] if step["receipt_status"] == "CONFIRMED"]
    p95 = [value for step in steps for value in _p95_samples(step, profile["min_pool_requests"])]
    if not p95:
        raise CalibrationRefused(["P95_NOT_MEASURED"])
    lag = max(_lag_windows(step) for step in steps)
    observed_p95 = max(p95)
    proposed = dict(profile)
    proposed.update(
        version=_next_version(profile, versions),
        status="measured",
        p95_ms_max=math.ceil(observed_p95 * P95_MARGIN / P95_STEP_MS) * P95_STEP_MS,
        unknown_limit=max(UNKNOWN_FLOOR, lag + UNKNOWN_MARGIN),
        basis=(f"T18 실행 {alignment['run_id']}(Edge 경유, 단계 {sorted({s['weight_target'] for s in steps})})의 align 결과. "
               f"가중치가 있고 풀 요청 {profile['min_pool_requests']}건 이상인 창의 p95 최대 {observed_p95}ms "
               f"× {P95_MARGIN} → {P95_STEP_MS}ms 단위 올림. "
               f"Edge 확인 뒤 새 가중치가 보이지 않은 창이 최대 {lag}개 이어짐 + {UNKNOWN_MARGIN}(최소 {UNKNOWN_FLOOR}). "
               "나머지 값은 이전 버전 그대로."),
    )
    return {"profile": proposed, "observed": {"p95_ms_max": observed_p95, "lag_windows_max": lag,
                                              "reflect_upper_bound_s": [s["reflect_upper_bound_s"] for s in steps]}}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print("사용: python -m bravo_observability.calibrate <weight-alignment.json> [threshold-profiles.json]",
              file=sys.stderr)
        return 2
    alignment = json.loads(Path(args[0]).read_text())
    source = Path(args[1]) if len(args) == 2 else PROFILES
    if not source.is_file():
        print("기준 프로필 파일이 없다. 레포의 docs/observability/threshold-profiles.json을 두 번째 인자로 준다.",
              file=sys.stderr)
        return 2
    wanted = alignment.get("profile") or {}
    same_id = [p for p in json.loads(source.read_text())["profiles"] if p.get("id") == wanted.get("id")]
    profile = next((p for p in same_id if p.get("version") == wanted.get("version")), None)
    try:
        if profile is None:
            raise CalibrationRefused(["PROFILE_NOT_FOUND"])
        result = calibrate(alignment, profile, versions=[p["version"] for p in same_id])
    except CalibrationRefused as refused:
        print(json.dumps({"refused": refused.args[0]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
