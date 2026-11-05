"""가중치 영수증과 창을 같은 실행 ID로 맞춘다. 단계별 반영 상한과 환경별 p95를 낸다.

판정은 T10이 저장한 것이 기준이다. 여기의 판정은 T36 코드로 다시 읽은 참고값이다.
provider가 확인한 시각은 실제 반영이 아니므로, 반영은 그 뒤의 창으로만 말한다.
"""

from datetime import datetime, timedelta

from bravo_observability import judge, receive

# 이 사유가 있으면 그 창은 새 가중치가 반영됐다는 증거가 아니다.
NOT_EVIDENCE = {"INSUFFICIENT_SAMPLES", "NOT_APPLIED", "WINDOW_BEFORE_CHANGE"}


class AlignmentError(ValueError):
    """다른 실행의 기록이거나 맞출 수 없는 입력이다."""


def _ts(value: object) -> datetime:
    if not isinstance(value, str):
        raise AlignmentError("영수증의 시각 형식이 잘못됐다")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AlignmentError("영수증의 시각 형식이 잘못됐다") from None
    if parsed.utcoffset() is None:
        raise AlignmentError("영수증의 시각에 시간대가 없다")
    return parsed


def _read_window(window: dict, receipt: dict, profile: dict) -> dict:
    # 판정 내용만 본다. 각 창이 끝난 1초 뒤 받아 2초 뒤 읽은 것으로 쳐서 시각 사유를 빼낸다.
    end = datetime.fromisoformat(window["window"]["end"])
    received = receive(window, (end + timedelta(seconds=1)).isoformat())
    result = judge(
        profile,
        received,
        plan_id=window["plan_id"],
        state="SHIFT",
        weight_target=receipt["weight_target"],
        observe_from=receipt["request_at"],
        decided_at=(end + timedelta(seconds=2)).isoformat(),
        prior_unknown=0,
    )
    source, target = window["pools"]["source"], window["pools"]["target"]
    attributed = source["requests"] + target["requests"]
    return {
        "start": window["window"]["start"],
        "end": window["window"]["end"],
        "reference_verdict": result["verdict"],
        "reasons": result["reasons"],
        "target_share": round(target["requests"] / attributed, 3) if attributed else None,
        "source": {k: source[k] for k in ("requests", "http_5xx", "p95_ms")},
        "target": {k: target[k] for k in ("requests", "http_5xx", "p95_ms")},
        "unattributed": sum(window["unattributed"].values()),
    }


def _p95_max(rows: list[dict], pool: str) -> float | None:
    values = [row[pool]["p95_ms"] for row in rows if row[pool]["p95_ms"] is not None]
    return round(max(values), 3) if values else None


def _step(receipt: dict, next_request: datetime | None, windows: list[dict], profile: dict) -> dict:
    requested = _ts(receipt["request_at"])
    confirmed = receipt.get("confirmed_at")
    step = {
        "weight_target": receipt["weight_target"],
        "reason": receipt.get("reason"),
        "revision": receipt.get("revision"),
        "receipt_status": receipt["status"],
        "request_at": receipt["request_at"],
        # 설정이 저장됐다는 시각이다. 실제 분배 반영이 아니다.
        "provider_confirmed_after_s": (
            round((_ts(confirmed) - requested).total_seconds(), 3) if confirmed else None
        ),
        "observation": "RECEIPT_NOT_CONFIRMED",
        "applied_by_window_end": None,
        "reflect_upper_bound_s": None,
        "windows": [],
        "source_p95_ms_max": None,
        "target_p95_ms_max": None,
    }
    if receipt["status"] != "CONFIRMED":
        return step
    # 다음 변경이 섞인 창과 이 변경 전이 섞인 창은 어느 단계의 증거도 아니다.
    inside = [
        w for w in windows
        if datetime.fromisoformat(w["window"]["start"]) >= requested
        and (next_request is None or datetime.fromisoformat(w["window"]["end"]) <= next_request)
    ]
    step["windows"] = [_read_window(w, receipt, profile) for w in inside]
    step["source_p95_ms_max"] = _p95_max(step["windows"], "source")
    step["target_p95_ms_max"] = _p95_max(step["windows"], "target")
    step["observation"] = "NOT_OBSERVED"
    for row in step["windows"]:
        if not NOT_EVIDENCE & set(row["reasons"]):
            step["observation"] = "OBSERVED"
            step["applied_by_window_end"] = row["end"]
            step["reflect_upper_bound_s"] = round(
                (datetime.fromisoformat(row["end"]) - requested).total_seconds(), 3
            )
            break
    return step


def align_run(report: dict, windows: list[dict], receipts: list[dict], profile: dict) -> dict:
    if not receipts:
        raise AlignmentError("가중치 영수증이 없다")
    try:
        for receipt in receipts:
            if (receipt["run_id"], receipt["plan_id"]) != (report["run_id"], report["plan_id"]):
                raise AlignmentError("영수증의 실행 ID 또는 계획이 이 실행과 다르다")
            if receipt["weight_target"] not in (0, 10, 50, 100) or not isinstance(receipt["status"], str):
                raise AlignmentError("영수증의 가중치나 상태가 잘못됐다")
        ordered = sorted(receipts, key=lambda r: _ts(r["request_at"]))
    except (KeyError, TypeError):
        raise AlignmentError("영수증에 필요한 값이 없다") from None
    next_requests = [_ts(r["request_at"]) for r in ordered[1:]] + [None]
    steps = [_step(r, n, windows, profile) for r, n in zip(ordered, next_requests, strict=True)]
    measurement = report["measurement"]
    return {
        "kind": "t18_weight_alignment",
        "run_id": report["run_id"],
        "plan_id": report["plan_id"],
        # 노트북 표시와 경고가 결과에서 떨어져 나가지 않게 함께 둔다.
        "measurement": {k: measurement[k] for k in ("path", "edge_measured", "generator")},
        "warnings": report["warnings"],
        "sli_usable_for_judgement": report["sli_usable_for_judgement"],
        "profile": {"id": profile["id"], "version": profile["version"]},
        "complete": all(step["observation"] == "OBSERVED" for step in steps),
        "steps": steps,
    }


def render_alignment(result: dict) -> str:
    lines = [
        f"실행 {result['run_id']} · 계획 {result['plan_id']} · 경로 {result['measurement']['path']}"
        f" · 실행 위치 {result['measurement']['generator']}",
        "가중치 | 결과 | 반영 상한(초) | provider 확인(초) | 출발지 p95 최대(ms) | 목적지 p95 최대(ms) | 창",
    ]
    for step in result["steps"]:
        lines.append(
            f"{step['weight_target']}% | {step['observation']} | {step['reflect_upper_bound_s']}"
            f" | {step['provider_confirmed_after_s']} | {step['source_p95_ms_max']}"
            f" | {step['target_p95_ms_max']} | {len(step['windows'])}"
        )
    return "\n".join(lines)
