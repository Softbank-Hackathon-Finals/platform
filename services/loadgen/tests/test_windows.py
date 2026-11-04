"""창 경계와 집계가 T36 계약(docs/observability/README.md 4절)을 지키는지 본다.

창 내용(분류 · p95)은 bravo_observability.build_window의 시험이 맡는다. 여기서는 T18이 그 함수에
올바르게 넘기는지와, 창을 어떻게 나누고 닫는지를 본다.
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bravo_observability import Record, receive

from bravo_loadgen.schema import SchemaViolation, validate_window
from bravo_loadgen.windows import WindowBuilder, check_continuity

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "observability"
T0 = datetime(2026, 11, 8, 5, 31, 10, tzinfo=UTC)
ENVS = {"aws": "source", "onprem": "target"}


def builder(window_seconds=10, plan_id="plan-demo"):
    return WindowBuilder(plan_id=plan_id, env_pools=ENVS, window_seconds=window_seconds, start_wall=T0)


def ok(env="aws", status=201, latency=5.0):
    return Record(env, status, latency)


def test_reproduces_the_contract_example_window():
    """계약 예시와 같은 요청이면 같은 창이 나온다."""
    example = json.loads((CONTRACT / "examples" / "window-shift-10.json").read_text())
    b = builder()
    for i in range(1800):
        b.add(0.001 * i, ok(latency=120.0 if i < 1720 else 90.0))
    for i in range(200):
        b.add(0.001 * i, ok(env="onprem", latency=260.0 if i < 195 else 100.0))
    assert receive(b.close(0), example["received_at"]) == example


def test_passes_registered_environments_to_the_reference_builder():
    """등록한 환경 이름이 출발지 · 목적지로 이어지고, 계약의 분류대로 세어진다."""
    b = builder()
    b.add(1, ok(latency=10.0))
    b.add(2, ok(status=503, latency=900.0))
    b.add(3, ok(env="onprem", status=409, latency=20.0))
    b.add(4, ok(env=None, status=200, latency=30.0))
    b.add(5, Record(None, None, 15000.0, "timeout"))
    b.add(6, Record(None, None, 1.0, "connect"))
    w = b.close(0)
    assert w["pools"]["source"] == {"requests": 2, "http_5xx": 1, "integrity_conflict": 0, "p95_ms": 900.0}
    assert w["pools"]["target"] == {"requests": 1, "http_5xx": 0, "integrity_conflict": 1, "p95_ms": 20.0}
    assert w["unattributed"] == {"timeout": 1, "connect": 1, "other": 1}
    assert w["plan_id"] == "plan-demo" and w["source"] == "loadgen"
    validate_window(w)


def test_buckets_by_completion_time_on_the_monotonic_clock():
    b = builder()
    b.add(9.999, ok())
    b.add(10.0, ok())
    first, second = b.close(0), b.close(1)
    assert first["pools"]["source"]["requests"] == 1
    assert second["pools"]["source"]["requests"] == 1
    assert first["window"]["end"] == second["window"]["start"]


def test_window_length_is_exact_even_with_sub_second_start():
    """판정은 창 길이를 시각 문자열로 비교한다(WINDOW_MISMATCH). 경계가 흔들리면 안 된다."""
    start = datetime(2026, 11, 8, 5, 31, 10, 123456, tzinfo=UTC)
    b = WindowBuilder(plan_id="p", env_pools=ENVS, window_seconds=10, start_wall=start)
    for k in range(5):
        w = b.close(k)
        s = datetime.fromisoformat(w["window"]["start"])
        e = datetime.fromisoformat(w["window"]["end"])
        assert (e - s).total_seconds() == 10
        assert w["window"]["start"].endswith("Z")
        validate_window(w)


def test_empty_window_is_emitted_with_null_p95_not_skipped():
    """창이 비어도 건너뛰지 않는다. 0건이 그대로 보여야 판정이 UNKNOWN으로 멈춘다."""
    w = builder().close(0)
    assert w["pools"]["source"] == {"requests": 0, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": None}
    validate_window(w)


def test_small_sample_is_reported_as_is_so_the_judge_can_return_unknown():
    profile = json.loads((CONTRACT / "threshold-profiles.json").read_text())["profiles"][0]
    b = builder(window_seconds=profile["window_seconds"])
    for i in range(50):
        b.add(i * 0.1, ok())
    w = b.close(0)
    total = w["pools"]["source"]["requests"] + w["pools"]["target"]["requests"]
    # 표본을 부풀리지 않는다. 계약 3절의 INSUFFICIENT_SAMPLES 조건이 그대로 성립한다.
    assert total == 50 < profile["min_requests"]
    assert w["pools"]["target"]["requests"] == 0 < profile["min_pool_requests"]


def test_late_observation_for_a_closed_window_is_counted_not_lost():
    b = builder()
    b.close(0)
    b.add(1, ok())
    assert b.late == 1


def test_conservation_counts_every_observation():
    b = builder()
    for i in range(7):
        b.add(i * 3.0, ok())  # 0, 3, 6, 9 | 12, 15, 18
    b.close(0)
    assert b.unemitted_requests() == 3


# --- 검증기가 정말로 거부하는가 (시험이 통과한다와 시험이 잰다는 다르다) ---


def _patched(base, ops):
    doc = copy.deepcopy(base)
    for op in ops:
        keys = op["path"].strip("/").split("/")
        node = doc
        for key in keys[:-1]:
            node = node[key]
        if op["op"] == "remove":
            del node[keys[-1]]
        else:
            node[keys[-1]] = op["value"]
    return doc


def test_validator_rejects_every_contract_schema_reject_case():
    cases = json.loads((CONTRACT / "contract-cases.json").read_text())["schema_reject"]
    base = json.loads((CONTRACT / "examples" / "window-shift-10.json").read_text())
    validate_window(base)
    assert cases["cases"], "계약의 거부 사례가 비어 있으면 이 시험은 아무것도 재지 않는다"
    for case in cases["cases"]:
        with pytest.raises(SchemaViolation):
            validate_window(_patched(base, case["patch"]))


def test_validator_rejects_bad_timestamp_format():
    """date-time 형식 검사는 기본으로 꺼져 있다. 켜져 있는지 직접 확인한다."""
    base = json.loads((CONTRACT / "examples" / "window-shift-10.json").read_text())
    base["window"]["start"] = "2026-11-08 05:31:10"
    with pytest.raises(SchemaViolation):
        validate_window(base)


def test_validator_rejects_other_document_kinds():
    """스키마 루트는 oneOf라 판정 기록도 통과할 수 있다. 창 요약만 받아야 한다."""
    profiles = json.loads((CONTRACT / "threshold-profiles.json").read_text())
    with pytest.raises(SchemaViolation):
        validate_window(profiles)


# --- 창 끊김 검사 ---


def _windows(n, seconds=10):
    return [builder(seconds).close(k) for k in range(n)]


def test_continuity_passes_contiguous_windows():
    assert check_continuity(_windows(4), window_seconds=10) == []


def test_continuity_reports_a_missing_window():
    ws = _windows(4)
    del ws[2]
    problems = check_continuity(ws, window_seconds=10)
    assert [p["kind"] for p in problems] == ["GAP"]
    assert problems[0]["after"] == ws[1]["window"]["end"]


def test_continuity_reports_overlap_and_wrong_length_and_duplicate():
    ws = _windows(3)
    ws[1]["window"]["start"] = "2026-11-08T05:31:15Z"
    kinds = {p["kind"] for p in check_continuity(ws, window_seconds=10)}
    assert {"OVERLAP", "WRONG_LENGTH"} <= kinds

    ws = _windows(2)
    assert "DUPLICATE" in {p["kind"] for p in check_continuity([ws[0], ws[0], ws[1]], window_seconds=10)}


def test_continuity_reports_mixed_plan_ids():
    a, b = builder(plan_id="plan-a").close(0), builder(plan_id="plan-b").close(1)
    assert "PLAN_CHANGED" in {p["kind"] for p in check_continuity([a, b], window_seconds=10)}
