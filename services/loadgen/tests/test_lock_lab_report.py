"""쓰기 잠금 실험의 판정 집계. 부하 구간의 창을 근거 없이 판정에서 빼지 않는지 본다."""

import lock_lab_report as report
import pytest

START = "2026-11-03T20:34:00+00:00"


@pytest.mark.parametrize(
    ("judge", "after_load", "expected"),
    [
        pytest.param([], None, "UNKNOWN", id="창이 없다"),
        pytest.param(["UNKNOWN"], None, "UNKNOWN", id="UNKNOWN 단독"),
        pytest.param(["UNKNOWN"], [True], "UNKNOWN", id="부하 구간의 창이 하나도 없다"),
        pytest.param(["PASS", "PASS"], None, "PASS", id="모두 PASS"),
        pytest.param(["PASS", "UNKNOWN"], None, "UNKNOWN", id="마지막 창의 근거가 없다"),
        pytest.param(["PASS", "UNKNOWN"], [False, False], "UNKNOWN", id="부하 중 UNKNOWN"),
        pytest.param(["UNKNOWN", "PASS"], [False, False], "UNKNOWN", id="앞쪽 창의 UNKNOWN"),
        pytest.param(["PASS", "PASS", "UNKNOWN"], [False, False, True], "PASS", id="종료 후 부분 창"),
        pytest.param(["PASS", "FAIL", "UNKNOWN"], None, "FAIL", id="부하 중 FAIL"),
        pytest.param(["PASS", "PASS", "FAIL"], [False, False, True], "FAIL", id="종료 후 창의 FAIL"),
    ],
)
def test_verdict(judge, after_load, expected):
    row = {"judge": judge}
    if after_load is not None:
        row["judge_after_load"] = after_load
    assert report.verdict(row) == expected


def test_verdict_rejects_evidence_of_a_different_length():
    with pytest.raises(ValueError):
        report.verdict({"judge": ["PASS", "UNKNOWN"], "judge_after_load": [True]})


def journal(first_sends, retries=(), ended_at=42.5):
    return [
        {"type": "run_start", "start_wall": START},
        *({"type": "attempt", "attempt_no": 1, "sent_at_s": at} for at in first_sends),
        *({"type": "attempt", "attempt_no": 2, "sent_at_s": at} for at in retries),
        {"type": "run_end", "ended_at_s": ended_at},
    ]


def window(start):
    return {"window": {"start": f"2026-11-03T20:34:{start:02d}Z"}}


def test_window_starting_after_last_first_send_is_after_load():
    windows = [window(0), window(10), window(20), window(30)]
    assert report.windows_after_load(windows, journal([0.0, 29.9])) == [False, False, False, True]


def test_retry_does_not_extend_the_load():
    assert report.windows_after_load([window(20), window(30)], journal([29.9], retries=[35.0])) == [False, True]


def test_window_is_during_load_when_first_send_lags_past_the_boundary():
    assert report.windows_after_load([window(20), window(30)], journal([30.5])) == [False, False]


def row(rtt, judge, rep=1, **fields):
    return {
        "variant": "baseline",
        "target_percent": 10,
        "rtt_ms": rtt,
        "rep": rep,
        "judge": judge,
        "unattributed": {"timeout": 0, "other": 0},
        "pools": {"source": {"p95_ms_worst_window": 10}},
        "app_errors": {"source": {}},
        "effective_rtt_ms": {"p50_ms": rtt + 2.0},
        **fields,
    }


def test_mark_shows_unknown_row():
    assert report.mark(row(25, ["PASS", "UNKNOWN"])) == "U"


def test_unknown_row_is_neither_pass_nor_fail_in_boundary():
    rows = [
        row(20, ["PASS"]),
        row(25, ["PASS", "UNKNOWN"]),
        row(35, ["FAIL"]),
    ]
    line = report.boundary_table(rows, ["baseline"]).splitlines()[-1]
    assert "| 20ms(22.0ms) | 35ms(37.0ms) |" in line
    assert "25ms U" in line


def test_row_with_evidence_counts_as_pass_in_boundary():
    rows = [
        row(20, ["PASS"]),
        row(25, ["PASS", "UNKNOWN"], judge_after_load=[False, True]),
        row(35, ["FAIL"]),
    ]
    line = report.boundary_table(rows, ["baseline"]).splitlines()[-1]
    assert "| 25ms(27.0ms) | 35ms(37.0ms) |" in line
