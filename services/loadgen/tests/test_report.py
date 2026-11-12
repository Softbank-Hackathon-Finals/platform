"""리포트: 오류율 · p95 · 환경 헤더 · 측정 경로를 사실대로 적는지 본다."""

import json

from bravo_loadgen.lock_probe import LockSample, summarize_lock_samples
from bravo_loadgen.report import build_report, exit_code, render_markdown

RUN_START = {
    "type": "run_start", "run_id": "t18-test", "plan_id": "plan-demo", "start_wall": "2026-11-03T00:00:00+00:00",
    "duration_s": 10, "write_rps": 50, "read_rps": 150, "window_seconds": 10,
    "attempt_timeout_s": 15, "retry_budget_s": 60, "env_pools": {"aws": "source", "onprem": "target"},
}
RUN_END = {"type": "run_end", "ended_at_s": 10.0, "scheduled_writes": 1, "scheduled_reads": 1}
KEY = "a0000000-0000-4000-8000-0000000000a1"
ROW = {"id": 1, "idempotency_key": KEY, "author": "loadgen-t18-test", "body": f"t18 run=t18-test req={KEY}"}
PREFLIGHT = {"passed": True, "health_rtt_ms": {"samples": 5, "p50": 31.5, "max": 48.25}, "env_values": {"aws": 5}}
# 판정 입력이 될 수 있는 유일한 조합: Edge 경유, 전용 EC2, 사전 점검 통과.
EDGE = {"measurement_path": "edge", "generator": "loadgen-ec2", "preflight": PREFLIGHT}


def write(**over):
    return {
        "type": "attempt", "kind": "write", "logical_id": KEY, "attempt_id": f"{KEY}.1", "attempt_no": 1,
        "lag_ms": 1.0, "sent_at_s": 0.1, "finished_at_s": 0.2, "latency_ms": 100.0, "outcome": "http",
        "status": 201, "env": "aws", "message_id": 1, "acked": True, "final": True, **over,
    }


def read(**over):
    return {
        "type": "attempt", "kind": "read", "logical_id": "r1", "attempt_id": "r1.1", "attempt_no": 1,
        "lag_ms": 3.0, "sent_at_s": 0.1, "finished_at_s": 0.15, "latency_ms": 50.0, "outcome": "http",
        "status": 200, "env": "onprem", "message_id": None, "acked": True, "final": True, **over,
    }


WINDOW = {"pools": {"source": {"requests": 1}, "target": {"requests": 1}},
          "unattributed": {"timeout": 0, "connect": 0, "other": 0}}


def report(records=None, rows=(ROW), **over):
    defaults = dict(
        records=records or [RUN_START, write(), read(), RUN_END],
        rows=list(rows) if rows is not None else None,
        windows=[], delivery_failed=0, invalid_windows=0, tail_requests=0, late_requests=0,
        continuity_problems=[], lock_probe=None, measurement_path="local-direct",
        target="http://127.0.0.1:8081", journal_sha256="ab" * 32,
    )
    return build_report(**{**defaults, **over})


def test_clean_report_passes_and_exits_zero():
    r = report(rows=[ROW])
    assert r["verdict"] == "PASS" and exit_code(r) == 0
    assert r["kind"] == "t18_run_report"
    assert r["reconciliation"]["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}


def test_failed_reconciliation_exits_one():
    r = report(rows=[])
    assert r["verdict"] == "FAIL" and exit_code(r) == 1


def test_no_database_means_unknown_not_pass():
    r = report(rows=None)
    assert r["verdict"] == "UNKNOWN" and exit_code(r) == 2
    assert r["reconciliation"] is None
    assert "NOT_RECONCILED" in r["reasons"]


def test_requests_summary_counts_per_kind():
    records = [RUN_START, write(), read(), read(status=503, env="aws", acked=False, latency_ms=900.0),
               read(outcome="timeout", status=None, env=None, acked=False, latency_ms=15000.0), RUN_END]
    r = report(records=records, rows=[ROW])
    assert r["requests"]["read"] == {
        "attempts": 3, "http_5xx": 1, "http_4xx": 0, "timeout": 1, "connect": 0, "p95_ms": 15000.0,
    }
    assert r["requests"]["write"]["attempts"] == 1
    # 응답을 못 받은 시도는 HTTP 오류율에 들어간다.
    assert r["http_error_rate"] == 0.5


def test_missing_env_header_counts_all_as_contract_errors_and_warns():
    """헤더가 없으면 계약상 전부 오류다. 숨기지 않고 두 오류율을 나란히 보인다."""
    records = [RUN_START, write(env=None), read(env=None), RUN_END]
    r = report(records=records, rows=[ROW])
    assert r["env_header"]["responses_without_header"] == 2
    assert r["contract_error_rate"] == 1.0 and r["http_error_rate"] == 0.0
    assert "ENV_HEADER_MISSING" in r["warnings"]
    # NFR-2 대조 자체는 환경 헤더와 무관하다.
    assert r["verdict"] == "PASS"
    assert r["sli_usable_for_judgement"] is False


def test_unregistered_env_value_is_reported():
    records = [RUN_START, write(env="staging"), read(), RUN_END]
    r = report(records=records, rows=[ROW])
    assert r["env_header"]["unregistered_values"] == {"staging": 1}
    assert "ENV_HEADER_UNREGISTERED" in r["warnings"]


def test_local_path_is_never_reported_as_edge():
    r = report(rows=[ROW])
    assert r["measurement"]["path"] == "local-direct"
    assert r["measurement"]["edge_measured"] is False
    assert r["sli_usable_for_judgement"] is False
    assert "NOT_EDGE_MEASURED" in r["warnings"]


def test_edge_path_with_headers_is_usable():
    r = report(rows=[ROW], **EDGE, target="https://edge.example.invalid", windows=[WINDOW])
    assert r["warnings"] == []
    assert r["measurement"]["edge_measured"] is True
    assert r["measurement"]["generator"] == "loadgen-ec2"
    assert r["sli_usable_for_judgement"] is True


def test_laptop_is_labelled_and_never_usable_for_judgement():
    """회선과 노트북 상태가 값에 섞인다. Edge를 지났어도 판정 입력으로 표시하지 않는다."""
    r = report(rows=[ROW], **{**EDGE, "generator": "laptop"}, windows=[WINDOW])
    assert r["warnings"] == ["GENERATOR_NOT_EC2"]
    assert r["measurement"] == {
        "path": "edge", "edge_measured": True, "generator": "laptop", "connections": "keepalive",
        "preflight": PREFLIGHT, "sign_in": None,
    }
    assert r["sli_usable_for_judgement"] is False
    text = render_markdown(r)
    assert "`laptop`" in text and "회선" in text and "판정 근거로 쓰지 않는다" in text


def test_line_baseline_from_preflight_is_shown_in_the_report():
    text = render_markdown(report(rows=[ROW], **EDGE, windows=[WINDOW]))
    assert "p50 31.5ms" in text and "최대 48.25ms" in text


def test_laptop_lag_shows_both_limits():
    records = [RUN_START, write(lag_ms=500.0), read(lag_ms=500.0), RUN_END]
    r = report(records=records, rows=[ROW], **{**EDGE, "generator": "laptop"}, windows=[WINDOW])
    assert set(r["warnings"]) == {"GENERATOR_NOT_EC2", "GENERATOR_LAGGING"}


def test_edge_without_a_declared_generator_is_not_usable():
    """generator가 없던 예전 실행을 다시 대조해도 판정 입력이 되지 않는다."""
    r = report(rows=[ROW], measurement_path="edge", preflight=PREFLIGHT, windows=[WINDOW])
    assert r["measurement"]["generator"] is None
    assert r["warnings"] == ["GENERATOR_NOT_EC2"]
    assert r["sli_usable_for_judgement"] is False


def test_edge_without_preflight_is_not_usable():
    """edge는 사람이 적은 값이다. 사전 점검이 Cloudflare 응답을 보지 않았으면 믿지 않는다."""
    r = report(rows=[ROW], measurement_path="edge", generator="loadgen-ec2", windows=[WINDOW])
    assert r["warnings"] == ["PREFLIGHT_SKIPPED"]
    assert r["sli_usable_for_judgement"] is False


def test_local_path_does_not_get_edge_only_warnings():
    r = report(rows=[ROW], generator="laptop", windows=[WINDOW])
    assert r["warnings"] == ["NOT_EDGE_MEASURED"]


def test_window_problems_are_reported():
    gap = [{"kind": "GAP", "after": "a", "next": "b"}]
    r = report(rows=[ROW], continuity_problems=gap, delivery_failed=2, invalid_windows=1, late_requests=3)
    assert r["windows"]["continuity_problems"] == gap
    assert {"WINDOW_GAP", "WINDOW_DELIVERY_FAILED", "WINDOW_INVALID", "LATE_OBSERVATIONS"} <= set(r["warnings"])
    assert r["sli_usable_for_judgement"] is False


def test_window_conservation_catches_uncounted_attempts():
    """창 합계 + 꼬리 + 늦은 관측이 전체 시도와 다르면 집계가 샌 것이다."""
    w = {"pools": {"source": {"requests": 1}, "target": {"requests": 0}},
         "unattributed": {"timeout": 0, "connect": 0, "other": 0}}
    r = report(rows=[ROW], windows=[w], tail_requests=0)
    assert r["windows"]["conserved"] is False and "WINDOW_COUNT_MISMATCH" in r["warnings"]
    r = report(rows=[ROW], windows=[w], tail_requests=1)
    assert r["windows"]["conserved"] is True


def test_generator_lag_warns_and_blocks_judgement_use():
    r = report(records=[RUN_START, write(lag_ms=500.0), read(lag_ms=500.0), RUN_END], rows=[ROW],
               **EDGE, windows=[WINDOW])
    assert r["warnings"] == ["GENERATOR_LAGGING"]
    assert r["load"]["scheduler_lag_ms"]["p95"] == 500.0
    assert r["sli_usable_for_judgement"] is False


def test_a_short_generator_stall_warns_even_when_p95_is_fine():
    """긴 실행의 10초 정지는 p95에 안 잡힌다. 최댓값으로도 본다."""
    reads = [read(logical_id=f"r{i}", lag_ms=1.0) for i in range(99)]
    r = report(records=[RUN_START, write(lag_ms=10000.0), *reads, RUN_END], rows=[ROW])
    assert r["load"]["scheduler_lag_ms"] == {"p95": 1.0, "max": 10000.0}
    assert "GENERATOR_LAGGING" in r["warnings"]


def test_achieved_rate_is_measured_from_the_journal():
    records = [RUN_START, write(sent_at_s=0.0), write(sent_at_s=9.9, attempt_no=2, attempt_id=f"{KEY}.2", lag_ms=None),
               read(sent_at_s=0.0), RUN_END]
    r = report(records=records, rows=[ROW])
    # 재시도(2번째 시도)는 예정된 쓰기 속도에 넣지 않는다.
    assert r["load"]["achieved_write_rps"] == 0.1


def test_achieved_rate_drops_when_sending_ran_past_the_load_time():
    """보내기가 밀리면 실제 속도는 명목 시간으로 나눈 값보다 낮다."""
    r = report(records=[RUN_START, write(sent_at_s=20.0), read(sent_at_s=40.0), RUN_END], rows=[ROW])
    assert r["load"]["achieved_write_rps"] == 0.05
    assert r["load"]["achieved_read_rps"] == 0.025


def test_write_gap_is_included():
    r = report(rows=[ROW])
    assert r["write_gap"]["position"] == "tail"
    assert r["write_gap"]["max_ms"] == 9800.0


def test_no_windows_is_not_usable_for_judgement():
    """창이 하나도 없으면 판정 입력으로 쓸 수 없다."""
    r = report(rows=[ROW], **EDGE, tail_requests=2)
    assert "NO_WINDOWS" in r["warnings"] and r["sli_usable_for_judgement"] is False


def test_fewer_windows_than_the_load_time_warns():
    start = {**RUN_START, "duration_s": 60}
    r = report(records=[start, write(), read(), RUN_END], rows=[ROW], **EDGE, windows=[WINDOW])
    assert "WINDOW_COUNT_SHORT" in r["warnings"] and r["sli_usable_for_judgement"] is False


def test_http_errors_warn_without_changing_the_write_verdict():
    """읽기가 전부 500이어도 NFR-2는 쓰기 대조다. 숨기지 않되 창에는 이미 오류로 들어가 있다."""
    records = [RUN_START, write(), read(status=500, acked=False), RUN_END]
    r = report(records=records, rows=[ROW], **EDGE, windows=[WINDOW])
    assert r["verdict"] == "PASS" and r["warnings"] == ["HTTP_ERRORS"]
    assert r["sli_usable_for_judgement"] is True


def test_stalled_lock_probe_warns():
    probe = summarize_lock_samples([LockSample("load", 0.1, 0, False, 5000.0)], key=1)
    r = report(rows=[ROW], lock_probe=probe)
    assert "LOCK_PROBE_STALLED" in r["warnings"]


def test_lock_probe_with_no_load_samples_warns():
    probe = summarize_lock_samples([LockSample("baseline", 1.0, 0, False, 1.0)], key=1)
    r = report(rows=[ROW], lock_probe=probe)
    assert "LOCK_PROBE_NO_SAMPLES" in r["warnings"]


def test_report_is_json_serializable_and_markdown_states_the_facts():
    r = report(rows=[])
    json.dumps(r)
    md = render_markdown(r)
    assert "FAIL" in md and "유실" in md and "local-direct" in md
    assert "Edge 경유 측정이 아니다" in md


def test_the_report_says_how_requests_reached_the_edge():
    """연결 방식이 분배 관측을 바꾼다(실제 LB는 연결마다 pool을 고른다). 예전 실행은 keepalive다."""
    from bravo_loadgen.report import render_markdown

    assert report(rows=[ROW], windows=[WINDOW])["measurement"]["connections"] == "keepalive"
    fresh = report(rows=[ROW], **EDGE, windows=[WINDOW], connections="fresh")
    assert fresh["measurement"]["connections"] == "fresh"
    assert "- 연결: `fresh`" in render_markdown(fresh)


def test_failed_token_refresh_is_a_generator_side_warning():
    """토큰이 만료되면 쓰기가 401로 끝나 대상의 실패처럼 보인다. 그 원인이 부하 생성기 쪽임을 남긴다."""
    healthy = report(rows=[ROW], windows=[WINDOW], sign_in={"kind": "oidc", "refreshes": 2, "refresh_failures": 0}, **EDGE)
    assert healthy["measurement"]["sign_in"]["kind"] == "oidc"
    assert healthy["warnings"] == [] and healthy["sli_usable_for_judgement"] is True
    failing = report(rows=[ROW], windows=[WINDOW], sign_in={"kind": "oidc", "refreshes": 0, "refresh_failures": 3}, **EDGE)
    assert "SIGN_IN_REFRESH_FAILED" in failing["warnings"]
    assert failing["sli_usable_for_judgement"] is False


def test_write_refused_for_sign_in_is_never_usable_for_judgement():
    refused = write(status=401, acked=False, message_id=None)
    r = report(records=[RUN_START, refused, read(), RUN_END], rows=[], windows=[WINDOW], **EDGE,
               withheld_windows=1, withheld_requests=0)
    assert "WRITE_AUTH_REJECTED" in r["warnings"] and r["sli_usable_for_judgement"] is False
    assert r["windows"]["withheld"] == 1


def test_withheld_requests_keep_the_window_count_conserved():
    r = report(records=[RUN_START, write(), read(), RUN_END], rows=[ROW], windows=[], **EDGE,
               withheld_windows=1, withheld_requests=2)
    assert r["windows"]["conserved"] is True and "WINDOW_COUNT_MISMATCH" not in r["warnings"]


def test_sign_in_is_absent_by_default():
    assert report(rows=None)["measurement"]["sign_in"] is None
