"""실행 하나의 리포트를 만든다. 측정하지 않은 값은 적지 않고, 판정 불가는 통과로 쓰지 않는다."""

from collections import Counter

from bravo_loadgen.reconcile import reconcile, write_gap
from bravo_loadgen.stats import nearest_rank

# 예정 시각보다 늦게 보내면 부하 생성기 자신이 병목이다. 창(10초)의 1% 기준으로 경고한다.
GENERATOR_LAG_P95_WARN_MS = 100.0
# 긴 실행의 짧은 정지는 p95에 안 잡힌다. 창의 10%(1초)를 넘게 밀리면 최댓값으로도 경고한다.
GENERATOR_LAG_MAX_WARN_MS = 1000.0
# 판정 입력의 온전함과 상관없는 경고. 오류는 창에 이미 오류로 들어가 있고, 잠금 측정은 창과 별개다.
NOT_ABOUT_THE_WINDOWS = {"HTTP_ERRORS", "LOCK_PROBE_NO_SAMPLES", "LOCK_PROBE_STALLED"}
# 판정 입력은 전용 호스트에서 잰다. 노트북 값에는 회선과 노트북 상태가 섞인다.
GENERATOR_FOR_JUDGEMENT = "loadgen-ec2"


def build_report(
    *,
    records: list[dict],
    rows: list[dict] | None,
    windows: list[dict],
    delivery_failed: int,
    invalid_windows: int,
    tail_requests: int,
    late_requests: int,
    continuity_problems: list[dict],
    lock_probe: dict | None,
    measurement_path: str,
    target: str,
    journal_sha256: str,
    # 둘 다 없던 예전 실행을 다시 대조할 수 있게 기본값을 둔다. 없으면 판정 입력으로 표시하지 않는다.
    generator: str | None = None,
    preflight: dict | None = None,
    connections: str = "keepalive",
) -> dict:
    start = next(r for r in records if r["type"] == "run_start")
    attempts = [r for r in records if r["type"] == "attempt"]
    env_pools = start["env_pools"]

    reconciliation = reconcile(records, rows) if rows is not None else None
    env_header = _env_header(attempts, env_pools)
    windows_summary = _windows_summary(
        windows, attempts, tail_requests, late_requests, delivery_failed, invalid_windows,
        continuity_problems,
    )
    load = _load_summary(start, attempts)
    edge_measured = measurement_path == "edge"

    warnings = []
    if not edge_measured:
        warnings.append("NOT_EDGE_MEASURED")
    if edge_measured and generator != GENERATOR_FOR_JUDGEMENT:
        warnings.append("GENERATOR_NOT_EC2")
    if edge_measured and preflight is None:
        warnings.append("PREFLIGHT_SKIPPED")
    if env_header["responses_without_header"]:
        warnings.append("ENV_HEADER_MISSING")
    if env_header["unregistered_values"]:
        warnings.append("ENV_HEADER_UNREGISTERED")
    if continuity_problems:
        warnings.append("WINDOW_GAP")
    if delivery_failed:
        warnings.append("WINDOW_DELIVERY_FAILED")
    if invalid_windows:
        warnings.append("WINDOW_INVALID")
    if late_requests:
        warnings.append("LATE_OBSERVATIONS")
    if not windows_summary["conserved"]:
        warnings.append("WINDOW_COUNT_MISMATCH")
    lag = load["scheduler_lag_ms"]
    if lag and (lag["p95"] > GENERATOR_LAG_P95_WARN_MS or lag["max"] > GENERATOR_LAG_MAX_WARN_MS):
        warnings.append("GENERATOR_LAGGING")
    if not windows:
        warnings.append("NO_WINDOWS")
    elif len(windows) + invalid_windows < start["duration_s"] // start["window_seconds"]:
        warnings.append("WINDOW_COUNT_SHORT")
    if any(_is_http_error(a) for a in attempts):
        warnings.append("HTTP_ERRORS")
    if lock_probe is not None:
        if not lock_probe["load"]["samples"]:
            warnings.append("LOCK_PROBE_NO_SAMPLES")
        elif lock_probe["load"]["stalled"] or lock_probe["load"]["timed_out"]:
            warnings.append("LOCK_PROBE_STALLED")

    verdict = reconciliation["verdict"] if reconciliation else "UNKNOWN"
    reasons = reconciliation["reasons"] if reconciliation else ["NOT_RECONCILED"]
    total = len(attempts)
    return {
        "kind": "t18_run_report",
        "schema_version": "1",
        "run_id": start["run_id"],
        "plan_id": start["plan_id"],
        "started_at": start["start_wall"],
        "target": target,
        "measurement": {
            "path": measurement_path,
            "edge_measured": edge_measured,
            "generator": generator,
            "connections": connections,
            "preflight": preflight,
        },
        "journal_sha256": journal_sha256,
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        # 판정 입력으로 쓸 수 있는 조건: 사전 점검을 통과한 전용 호스트의 Edge 경유 측정이고,
        # 환경을 모두 알 수 있고, 창이 온전하다.
        "sli_usable_for_judgement": edge_measured and not set(warnings) - NOT_ABOUT_THE_WINDOWS,
        "load": load,
        "requests": {kind: _kind_summary(attempts, kind) for kind in ("write", "read")},
        "http_error_rate": _rate(sum(_is_http_error(a) for a in attempts), total),
        "contract_error_rate": _rate(sum(_is_contract_error(a, env_pools) for a in attempts), total),
        "p95_ms": nearest_rank([a["latency_ms"] for a in attempts]),
        "env_header": env_header,
        "reconciliation": reconciliation,
        "write_gap": write_gap(attempts, load_end_s=start["duration_s"]),
        "lock_probe": lock_probe,
        "windows": windows_summary,
    }


def exit_code(report: dict) -> int:
    """0 통과 · 1 실패 · 2 시험 불가."""
    return {"PASS": 0, "FAIL": 1}.get(report["verdict"], 2)


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 6) if total else None


def _is_http_error(attempt: dict) -> bool:
    return attempt["outcome"] != "http" or attempt["status"] >= 500


def _is_contract_error(attempt: dict, env_pools: dict[str, str]) -> bool:
    """계약 3절의 errors: 5xx와 환경 미확인 요청 전부."""
    return _is_http_error(attempt) or attempt["env"] not in env_pools


def _kind_summary(attempts: list[dict], kind: str) -> dict:
    mine = [a for a in attempts if a["kind"] == kind]
    http = [a for a in mine if a["outcome"] == "http"]
    return {
        "attempts": len(mine),
        "http_5xx": sum(a["status"] >= 500 for a in http),
        "http_4xx": sum(400 <= a["status"] < 500 for a in http),
        "timeout": sum(a["outcome"] == "timeout" for a in mine),
        "connect": sum(a["outcome"] == "connect" for a in mine),
        "p95_ms": nearest_rank([a["latency_ms"] for a in mine]),
    }


def _env_header(attempts: list[dict], env_pools: dict[str, str]) -> dict:
    http = [a for a in attempts if a["outcome"] == "http"]
    values = Counter(a["env"] for a in http if a["env"] is not None)
    return {
        "responses": len(http),
        "responses_without_header": sum(a["env"] is None for a in http),
        "registered_values": {k: v for k, v in values.items() if k in env_pools},
        "unregistered_values": {k: v for k, v in values.items() if k not in env_pools},
    }


def _load_summary(start: dict, attempts: list[dict]) -> dict:
    firsts = [a for a in attempts if a["attempt_no"] == 1]
    lags = [a["lag_ms"] for a in firsts if a["lag_ms"] is not None]
    duration = start["duration_s"]

    def achieved(kind: str) -> float:
        # 보내기가 밀려 부하 시간을 넘기면 실제 속도는 명목 시간으로 나눈 값보다 낮다.
        sent = [a["sent_at_s"] for a in firsts if a["kind"] == kind]
        return round(len(sent) / max([duration, *sent]), 3)

    return {
        "duration_s": duration,
        "target_write_rps": start["write_rps"],
        "target_read_rps": start["read_rps"],
        "achieved_write_rps": achieved("write"),
        "achieved_read_rps": achieved("read"),
        "attempt_timeout_s": start["attempt_timeout_s"],
        "retry_budget_s": start["retry_budget_s"],
        "scheduler_lag_ms": {"p95": nearest_rank(lags), "max": max(lags)} if lags else None,
    }


def _windows_summary(
    windows, attempts, tail_requests, late_requests, delivery_failed, invalid_windows,
    continuity_problems,
) -> dict:
    counted = sum(
        w["pools"]["source"]["requests"]
        + w["pools"]["target"]["requests"]
        + sum(w["unattributed"].values())
        for w in windows
    )
    return {
        "emitted": len(windows),
        "requests_in_windows": counted,
        "tail_requests_not_in_a_window": tail_requests,
        "late_requests": late_requests,
        "conserved": counted + tail_requests + late_requests == len(attempts),
        "delivery_failed": delivery_failed,
        "invalid": invalid_windows,
        "continuity_problems": continuity_problems,
        "pools_total": {
            pool: sum(w["pools"][pool]["requests"] for w in windows) for pool in ("source", "target")
        },
        "unattributed_total": {
            kind: sum(w["unattributed"][kind] for w in windows)
            for kind in ("timeout", "connect", "other")
        },
    }


def render_markdown(report: dict) -> str:
    rec = report["reconciliation"]
    measurement = report["measurement"]
    lines = [
        f"# T18 실행 리포트 `{report['run_id']}`",
        "",
        f"- 판정: **{report['verdict']}** (사유: {', '.join(report['reasons']) or '없음'})",
        f"- 경고: {', '.join(report['warnings']) or '없음'}",
        f"- 측정 경로: `{measurement['path']}`",
    ]
    if not measurement["edge_measured"]:
        lines.append("- **Edge 경유 측정이 아니다.** 이 수치를 SHIFT · WATCH 판정 근거로 쓰지 않는다.")
    if measurement["generator"]:
        lines.append(f"- 실행 위치: `{measurement['generator']}`")
    lines.append(f"- 연결: `{measurement.get('connections', 'keepalive')}`")
    if measurement["generator"] == "laptop":
        lines.append("- **노트북에서 잰 값이다. 회선 지연이 p95에 함께 들어가고, 노트북이 바쁘거나 잠들면 생성기 지연이 된다. 판정 근거로 쓰지 않는다.**")
    rtt = measurement["preflight"]["health_rtt_ms"] if measurement["preflight"] else None
    if rtt:
        lines.append(f"- 부하 전 `/api/health/db` 왕복: p50 {rtt['p50']}ms, 최대 {rtt['max']}ms ({rtt['samples']}회, 회선 + 앱 + DB)")
    if rec:
        counts = rec["counts"]
        lines += [
            "",
            "## 전수 대조 (NFR-2)",
            "",
            "| 항목 | 건수 |",
            "|---|---:|",
            f"| 논리 쓰기 요청 | {counts['logical_writes']} |",
            f"| 시도 | {counts['attempts']} |",
            f"| 확인됨 | {counts['confirmed']} |",
            f"| **실패** | {rec['nfr2']['failed']} |",
            f"| - 응답 없음, 저장 안 됨 | {counts['failed_not_stored']} |",
            f"| - 응답 없음, 저장됨 | {counts['failed_but_stored']} |",
            f"| - 멱등 충돌(409) | {counts['integrity_conflict']} |",
            f"| **유실** | {rec['nfr2']['lost']} |",
            f"| **중복** | {rec['nfr2']['duplicated']} |",
            f"| 내용 불일치 | {counts['content_mismatch']} |",
            f"| 요청한 적 없는 행 | {counts['phantom_rows']} |",
            f"| 재시도한 요청 | {counts['retried']} |",
            f"| 같은 키로 응답 복구 | {counts['idempotent_replays']} |",
        ]
    else:
        lines += ["", "## 전수 대조 (NFR-2)", "", "하지 못했다. DB를 읽지 않았으므로 판정할 수 없다."]
    gap = report["write_gap"]
    load = report["load"]
    lines += [
        "",
        "## 부하와 지표",
        "",
        f"- 목표 쓰기 {load['target_write_rps']} · 읽기 {load['target_read_rps']} req/s,"
        f" 실제 {load['achieved_write_rps']} · {load['achieved_read_rps']} req/s, {load['duration_s']}초",
        f"- HTTP 오류율 {report['http_error_rate']}, 계약 오류율(환경 미확인 포함)"
        f" {report['contract_error_rate']}, 전체 p95 {report['p95_ms']} ms",
        f"- 쓰기 응답 최대 간격 {gap['max_ms']} ms ({gap['position']}, {gap['from_s']}~{gap['to_s']}초)",
        f"- 환경 헤더 없는 응답 {report['env_header']['responses_without_header']}"
        f" / {report['env_header']['responses']}",
    ]
    probe = report["lock_probe"]
    if probe and probe["load"]["wait_ms"]:
        lines.append(
            f"- 쓰기 잠금 대기(부하 중) p50 {probe['load']['wait_ms']['p50']} ·"
            f" p95 {probe['load']['wait_ms']['p95']} · 최대 {probe['load']['wait_ms']['max']} ms"
            f" (표본 {probe['load']['samples']}, 시간 초과 {probe['load']['timed_out']},"
            f" 멈춤 {probe['load']['stalled']})"
        )
    windows = report["windows"]
    lines += [
        "",
        "## 창 요약",
        "",
        f"- 낸 창 {windows['emitted']}개, 창 안 요청 {windows['requests_in_windows']}건,"
        f" 창에 못 든 꼬리 {windows['tail_requests_not_in_a_window']}건,"
        f" 집계 일치 {windows['conserved']}",
        f"- 끊김 {len(windows['continuity_problems'])}건, 전달 실패 {windows['delivery_failed']}건,"
        f" 스키마 위반 {windows['invalid']}건",
        f"- 판정 입력으로 쓸 수 있음: {report['sli_usable_for_judgement']}",
        "",
    ]
    return "\n".join(lines)
