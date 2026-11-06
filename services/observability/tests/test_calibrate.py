"""기준 프로필 실측 확정 계산(calibrate)을 T18 align 결과 형식으로 시험한다."""

import copy
import json
from pathlib import Path

import pytest

from bravo_observability import judge
from bravo_observability.calibrate import CalibrationRefused, calibrate, main

DOCS = Path(__file__).resolve().parents[3] / "docs/observability"
PROFILE = next(p for p in json.loads((DOCS / "threshold-profiles.json").read_text())["profiles"]
               if p["id"] == "demo-shift")


def row(start, reasons, source_p95=120, target_p95=None):
    return {"start": start, "end": start[:-3] + f"{int(start[-3:-1]) + 10:02d}Z", "reference_verdict": "PASS",
            "reasons": reasons, "target_share": None,
            "source": {"requests": 1800, "http_5xx": 0, "p95_ms": source_p95},
            "target": {"requests": 200, "http_5xx": 0, "p95_ms": target_p95}, "unattributed": 0}


def step(weight, request_at, confirmed_after, windows, source_p95=None, target_p95=None):
    return {"weight_target": weight, "reason": None, "revision": None, "receipt_status": "CONFIRMED",
            "request_at": request_at, "provider_confirmed_after_s": confirmed_after, "observation": "OBSERVED",
            "applied_by_window_end": None, "reflect_upper_bound_s": 25.0, "windows": windows,
            "source_p95_ms_max": source_p95, "target_p95_ms_max": target_p95}


ALIGNMENT = {
    "kind": "t18_weight_alignment", "run_id": "run-1", "plan_id": "plan-demo",
    "measurement": {"path": "edge", "edge_measured": True, "generator": "loadgen-ec2"},
    "warnings": [], "sli_usable_for_judgement": True,
    "profile": {"id": "demo-shift", "version": "1"}, "complete": True,
    "steps": [
        step(0, "2026-11-08T05:00:00+00:00", 2.0, [row("2026-11-08T05:00:10Z", [])], source_p95=140),
        # 10%: 05:01:02 확인. 05:01:10 창은 아직 반영 전(NOT_APPLIED) 1개, 05:01:20 창부터 반영.
        step(10, "2026-11-08T05:01:00+00:00", 2.0,
             [row("2026-11-08T05:01:10Z", ["NOT_APPLIED"]), row("2026-11-08T05:01:20Z", [])],
             source_p95=180, target_p95=300),
        # 50%: 확인 전에 시작한 창은 세지 않는다. 확인 뒤 NOT_APPLIED · 표본 부족이 2개 이어진다.
        step(50, "2026-11-08T05:03:00+00:00", 5.0,
             [row("2026-11-08T05:03:00Z", ["NOT_APPLIED"]), row("2026-11-08T05:03:10Z", ["NOT_APPLIED"]),
              row("2026-11-08T05:03:20Z", ["INSUFFICIENT_SAMPLES"]), row("2026-11-08T05:03:30Z", [])],
             source_p95=200, target_p95=260),
        step(100, "2026-11-08T05:05:00+00:00", 2.0, [row("2026-11-08T05:05:10Z", [])], target_p95=310),
        # 되돌림 0%: 목적지 p95(900)는 가중치 0이라 기준에 넣지 않는다.
        step(0, "2026-11-08T05:07:00+00:00", 2.0, [row("2026-11-08T05:07:10Z", [])],
             source_p95=150, target_p95=900),
    ],
}


def test_proposes_p95_and_unknown_limit_from_the_measured_run():
    result = calibrate(ALIGNMENT, PROFILE)
    proposed = result["profile"]
    assert result["observed"] == {"p95_ms_max": 310, "lag_windows_max": 2,
                                  "reflect_upper_bound_s": [25.0] * 5}
    assert proposed["p95_ms_max"] == 500  # 310 × 1.5 = 465 → 500
    assert proposed["unknown_limit"] == 4  # 2 + 2
    assert (proposed["version"], proposed["status"]) == ("2", "measured")
    assert "run-1" in proposed["basis"]
    unchanged = {k: v for k, v in PROFILE.items() if k not in ("version", "status", "p95_ms_max", "unknown_limit", "basis")}
    assert {k: proposed[k] for k in unchanged} == unchanged


def test_proposed_profile_is_accepted_by_judge():
    proposed = calibrate(ALIGNMENT, PROFILE)["profile"]
    decision = judge(proposed, None, plan_id="plan-demo", state="SHIFT", weight_target=10,
                     observe_from="2026-11-08T05:00:00Z", decided_at="2026-11-08T05:00:01Z", prior_unknown=0)
    assert decision["profile"] == {"id": "demo-shift", "version": "2"}


def test_unknown_limit_never_goes_below_the_floor():
    fast = copy.deepcopy(ALIGNMENT)
    for item in fast["steps"]:
        item["windows"] = [w for w in item["windows"] if not w["reasons"]]
    assert calibrate(fast, PROFILE)["profile"]["unknown_limit"] == 3


@pytest.mark.parametrize("change, reason", [
    (lambda a: a["measurement"].update(path="direct", edge_measured=False), "NOT_EDGE_MEASURED"),
    (lambda a: a.update(warnings=["GENERATOR_LAGGING"]), "RUN_HAS_WARNINGS"),
    (lambda a: a.update(sli_usable_for_judgement=False), "SLI_NOT_USABLE"),
    (lambda a: a.update(complete=False), "STEP_NOT_OBSERVED"),
    (lambda a: a.update(steps=[s for s in a["steps"] if s["weight_target"] != 50]), "STEPS_MISSING"),
    (lambda a: a["steps"][1].update(provider_confirmed_after_s=None), "CONFIRMATION_TIME_MISSING"),
])
def test_runs_that_cannot_set_the_baseline_are_refused(change, reason):
    alignment = copy.deepcopy(ALIGNMENT)
    change(alignment)
    with pytest.raises(CalibrationRefused) as refused:
        calibrate(alignment, PROFILE)
    assert reason in refused.value.args[0]


def test_cli_prints_the_proposal_or_the_refusal(tmp_path, capsys):
    path = tmp_path / "weight-alignment.json"
    path.write_text(json.dumps(ALIGNMENT))
    assert main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["profile"]["p95_ms_max"] == 500
    path.write_text(json.dumps(ALIGNMENT | {"warnings": ["ENV_HEADER_MISSING"]}))
    assert main([str(path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"refused": ["RUN_HAS_WARNINGS"]}
