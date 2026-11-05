"""가중치 영수증과 창 맞추기: 반영 상한을 부풀리거나 깎지 않고, 다른 실행을 섞지 않는지 본다."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from bravo_observability import Record, build_window

from bravo_loadgen.cli import main

START = datetime(2026, 11, 6, 1, 0, 0, tzinfo=UTC)
ENVS = {"aws": "source", "onprem": "target"}


def at(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def window(index: int, target_percent: int, *, total: int = 2000, latency: tuple = (5.0, 9.0)) -> dict:
    target = total * target_percent // 100
    records = [Record("onprem", 201, latency[1]) for _ in range(target)]
    records += [Record("aws", 201, latency[0]) for _ in range(total - target)]
    return build_window(
        records, plan_id="plan-demo", start=at(index * 10), end=at((index + 1) * 10), pools=ENVS
    )


def receipt(weight: int, seconds: float, **over) -> dict:
    return {
        "plan_id": "plan-demo", "run_id": "t18-joint", "operation_id": f"op-{weight}", "revision": 1,
        "weight_target": weight, "reason": "SHIFT", "lb_ref": "private-lb-id",
        "request_at": at(seconds), "confirmed_at": at(seconds + 2), "status": "CONFIRMED", **over,
    }


def write_run(out, windows, *, run_id="t18-joint", plan_id="plan-demo", measurement=None, warnings=()):
    out.mkdir()
    report = {
        "run_id": run_id, "plan_id": plan_id, "warnings": list(warnings), "sli_usable_for_judgement": False,
        "measurement": measurement or {"path": "local-mock-edge", "edge_measured": False, "generator": "laptop"},
    }
    (out / "report.json").write_text(json.dumps(report))
    (out / "windows.jsonl").write_text("".join(json.dumps(w) + "\n" for w in windows))


def run_align(tmp_path, windows, receipts, **over):
    out = tmp_path / "out"
    write_run(out, windows, **over)
    path = tmp_path / "receipts.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in receipts))
    code = main(["align", "--out", str(out), "--receipts", str(path)])
    result = out / "weight-alignment.json"
    return code, json.loads(result.read_text()) if result.exists() else None


def test_steps_report_the_first_window_that_shows_the_new_weight(tmp_path):
    # 5초에 10%로 바꿨다. 0~10초 창은 바뀌기 전이 섞이고, 10~20초 창이 첫 유효창이다.
    windows = [window(0, 0), window(1, 10), window(2, 10), window(3, 50), window(4, 50)]
    code, result = run_align(tmp_path, windows, [receipt(10, 5), receipt(50, 28, revision=2)])
    assert code == 0 and result["complete"] is True
    first, second = result["steps"]
    assert first["observation"] == "OBSERVED"
    # 바뀌기 전이 섞인 창도, 다음 변경이 섞인 창도 이 단계의 증거가 아니다.
    assert [w["start"] for w in first["windows"]] == [at(10)]
    assert first["applied_by_window_end"] == at(20)
    assert first["reflect_upper_bound_s"] == 15.0
    assert first["provider_confirmed_after_s"] == 2.0
    assert second["applied_by_window_end"] == at(40)
    assert second["reflect_upper_bound_s"] == 12.0


def test_each_step_keeps_p95_per_environment(tmp_path):
    code, result = run_align(tmp_path, [window(1, 10, latency=(5.0, 9.0)), window(2, 10, latency=(6.0, 8.0))], [receipt(10, 5)])
    step = result["steps"][0]
    assert step["source_p95_ms_max"] == 6.0 and step["target_p95_ms_max"] == 9.0
    assert [w["target"]["p95_ms"] for w in step["windows"]] == [9.0, 8.0]
    assert [w["target_share"] for w in step["windows"]] == [0.1, 0.1]
    assert step["windows"][0]["reference_verdict"] == "PASS"


def test_window_that_crosses_the_next_change_belongs_to_neither_step(tmp_path):
    windows = [window(1, 10), window(2, 30)]
    _, result = run_align(tmp_path, windows, [receipt(10, 5), receipt(50, 25, revision=2)])
    first, second = result["steps"]
    assert [w["start"] for w in first["windows"]] == [at(10)]
    assert second["windows"] == []
    assert second["observation"] == "NOT_OBSERVED" and second["reflect_upper_bound_s"] is None


def test_weight_never_reflected_is_not_observed(tmp_path):
    code, result = run_align(tmp_path, [window(1, 0), window(2, 0)], [receipt(10, 5)])
    assert code == 1 and result["complete"] is False
    assert result["steps"][0]["observation"] == "NOT_OBSERVED"
    assert result["steps"][0]["reflect_upper_bound_s"] is None
    assert "NOT_APPLIED" in result["steps"][0]["windows"][0]["reasons"]


def test_wrong_share_with_enough_samples_does_not_prove_the_weight(tmp_path):
    """표본은 충분한데 아직 10%로 나뉘는 창은 50% 반영의 증거가 아니다."""
    code, result = run_align(tmp_path, [window(1, 10)], [receipt(50, 5)])
    assert code == 1
    assert result["steps"][0]["observation"] == "NOT_OBSERVED"
    assert result["steps"][0]["windows"][0]["reasons"] == ["NOT_APPLIED"]


def test_low_sample_window_does_not_prove_the_weight(tmp_path):
    """짧은 시험 실행처럼 표본이 적으면 비율이 맞아 보여도 반영 증거로 쓰지 않는다."""
    code, result = run_align(tmp_path, [window(1, 10, total=200)], [receipt(10, 5)])
    assert code == 1
    assert result["steps"][0]["observation"] == "NOT_OBSERVED"
    assert "INSUFFICIENT_SAMPLES" in result["steps"][0]["windows"][0]["reasons"]


@pytest.mark.parametrize("status", ["UNCERTAIN", "AWAITING_READBACK", "SUPERSEDED"])
def test_unconfirmed_receipt_is_never_counted_as_applied(tmp_path, status):
    code, result = run_align(tmp_path, [window(1, 10)], [receipt(10, 5, status=status, confirmed_at=None)])
    assert code == 1
    assert result["steps"][0]["observation"] == "RECEIPT_NOT_CONFIRMED"
    assert result["steps"][0]["reflect_upper_bound_s"] is None


def test_failing_window_still_counts_as_reflected_and_shows_the_verdict(tmp_path):
    """가중치가 반영된 것과 그 환경이 정상인 것은 다른 사실이다."""
    slow = window(1, 10, latency=(5.0, 9000.0))
    code, result = run_align(tmp_path, [slow], [receipt(10, 5)])
    assert code == 0
    assert result["steps"][0]["observation"] == "OBSERVED"
    assert result["steps"][0]["windows"][0]["reference_verdict"] == "FAIL"


def test_receipts_are_ordered_by_request_time(tmp_path):
    windows = [window(1, 10), window(3, 50)]
    _, result = run_align(tmp_path, windows, [receipt(50, 25, revision=2), receipt(10, 5)])
    assert [s["weight_target"] for s in result["steps"]] == [10, 50]


@pytest.mark.parametrize("field", ["run_id", "plan_id"])
def test_receipt_from_another_run_is_refused(tmp_path, capsys, field):
    code, result = run_align(tmp_path, [window(1, 10)], [receipt(10, 5, **{field: "other"})])
    assert code == 2 and result is None
    assert "실행 ID" in capsys.readouterr().err


def test_no_receipts_is_not_a_pass(tmp_path):
    code, result = run_align(tmp_path, [window(1, 10)], [])
    assert code == 2 and result is None


@pytest.mark.parametrize("broken", [{"request_at": "2026-11-06T01:00:00"}, {"request_at": None}, {"weight_target": None}])
def test_malformed_receipt_is_refused(tmp_path, broken):
    code, result = run_align(tmp_path, [window(1, 10)], [receipt(10, 5, **broken)])
    assert code == 2 and result is None


def test_missing_run_output_is_refused(tmp_path):
    path = tmp_path / "receipts.jsonl"
    path.write_text(json.dumps(receipt(10, 5)) + "\n")
    assert main(["align", "--out", str(tmp_path / "none"), "--receipts", str(path)]) == 2


def test_the_measurement_label_stays_with_the_result_and_private_ids_do_not(tmp_path):
    laptop = {"path": "edge", "edge_measured": True, "generator": "laptop", "preflight": {"checks": []}}
    _, result = run_align(tmp_path, [window(1, 10)], [receipt(10, 5)], measurement=laptop, warnings=["GENERATOR_NOT_EC2"])
    assert result["measurement"] == {"path": "edge", "edge_measured": True, "generator": "laptop"}
    assert result["warnings"] == ["GENERATOR_NOT_EC2"]
    assert result["sli_usable_for_judgement"] is False
    text = json.dumps(result)
    assert "private-lb-id" not in text and "op-10" not in text
