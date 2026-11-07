"""부하 기록과 DB 행을 전수로 대조한다 (NFR-2). DB를 읽는 일과 분리한 순수 계산이다.

논리 요청 하나는 아래 상태 중 정확히 하나가 된다.

| 상태 | 뜻 | NFR-2 |
|---|---|---|
| confirmed | 확인 응답을 받았고 DB에 내용이 같은 행이 하나 있다 | - |
| lost | 확인 응답을 받았는데 행이 없다 | 유실 |
| duplicated | 행이 둘 이상이다 | 중복 |
| failed_not_stored | 끝내 확인 응답을 못 받았고 행도 없다 | 실패 |
| failed_but_stored | 끝내 확인 응답을 못 받았지만 행은 있다 | 실패 |
| integrity_conflict | 409를 받았다 (같은 키에 다른 내용) | 실패 |
| content_mismatch | 행은 하나인데 내용이나 응답의 메시지 ID가 다르다 | 판정 FAIL |
"""

import re
from collections import defaultdict

from bravo_loadgen.journal import message_body, run_author

SAMPLE_LIMIT = 20
_BODY_MARKER = re.compile(r"^t18 run=[a-z0-9-]+ req=([0-9a-f-]{36})$")
STATES = (
    "confirmed",
    "lost",
    "duplicated",
    "failed_not_stored",
    "failed_but_stored",
    "integrity_conflict",
    "content_mismatch",
)


def reconcile(records: list[dict], rows: list[dict]) -> dict:
    start = next((r for r in records if r["type"] == "run_start"), None)
    run_id = start["run_id"] if start else None
    author = run_author(start) if start else None
    end = next((r for r in records if r["type"] == "run_end"), None)
    attempts = defaultdict(list)
    for record in records:
        if record["type"] == "attempt" and record["kind"] == "write":
            attempts[record["logical_id"]].append(record)

    rows_by_logical, phantoms = _assign_rows(rows, attempts.keys())
    states = {
        logical: _state(run_id, author, logical, tries, rows_by_logical[logical])
        for logical, tries in attempts.items()
    }

    counts = dict.fromkeys(STATES, 0)
    samples: dict[str, list[str]] = defaultdict(list)
    for logical, state in states.items():
        counts[state] += 1
        if state != "confirmed" and len(samples[state]) < SAMPLE_LIMIT:
            samples[state].append(logical)
    counts.update(
        logical_writes=len(attempts),
        attempts=sum(len(tries) for tries in attempts.values()),
        retried=sum(len(tries) > 1 for tries in attempts.values()),
        # 200은 "이미 저장된 메시지"라는 응답이다. 앞선 시도의 응답이 유실됐다가 같은 키로 복구됐다는 뜻이다.
        idempotent_replays=sum(
            any(t["acked"] and t["status"] == 200 for t in tries) for tries in attempts.values()
        ),
        duplicate_extra_rows=sum(max(len(v) - 1, 0) for v in rows_by_logical.values()),
        phantom_rows=len(phantoms),
    )
    if phantoms:
        samples["phantom_rows"] = [str(r["id"]) for r in phantoms[:SAMPLE_LIMIT]]

    failures = {
        "LOST": counts["lost"],
        "DUPLICATED": counts["duplicated"],
        "FAILED": counts["failed_not_stored"] + counts["failed_but_stored"],
        "INTEGRITY_CONFLICT": counts["integrity_conflict"],
        "CONTENT_MISMATCH": counts["content_mismatch"],
        "PHANTOM": counts["phantom_rows"],
    }
    reasons = [reason for reason, count in failures.items() if count]
    unknowns = []
    if not attempts:
        unknowns.append("NO_WRITES")
    if not _journal_complete(end, attempts):
        unknowns.append("JOURNAL_INCOMPLETE")
    # 없으면 0이 아니라 판정 불가다. 실패가 하나라도 보이면 그것이 먼저다.
    verdict = "FAIL" if reasons else "UNKNOWN" if unknowns else "PASS"
    return {
        "verdict": verdict,
        "reasons": sorted(reasons + unknowns),
        "nfr2": {
            "failed": failures["FAILED"] + counts["integrity_conflict"],
            "lost": counts["lost"],
            "duplicated": counts["duplicated"],
        },
        "counts": counts,
        "max_attempts": max((len(tries) for tries in attempts.values()), default=0),
        "samples": dict(samples),
    }


def _assign_rows(rows, logical_ids):
    by_logical: dict[str, list[dict]] = defaultdict(list)
    phantoms = []
    for row in rows:
        key = str(row["idempotency_key"])
        marker = _BODY_MARKER.match(row["body"] or "")
        if key in logical_ids:
            by_logical[key].append(row)
        elif marker and marker.group(1) in logical_ids:
            by_logical[marker.group(1)].append(row)
        else:
            phantoms.append(row)
    return by_logical, phantoms


def _state(
    run_id: str | None, author: str | None, logical: str, tries: list[dict], rows: list[dict]
) -> str:
    ack = next((t for t in tries if t["acked"]), None)
    if len(rows) > 1:
        return "duplicated"
    if ack is not None and not rows:
        return "lost"
    if rows:
        row = rows[0]
        same_content = (
            str(row["idempotency_key"]) == logical
            and row["author"] == author
            and row["body"] == message_body(run_id or "", logical)
        )
        if not same_content or (ack is not None and ack["message_id"] != row["id"]):
            return "content_mismatch"
    if any(t["status"] == 409 for t in tries):
        return "integrity_conflict"
    if ack is not None:
        return "confirmed"
    return "failed_but_stored" if rows else "failed_not_stored"


def _journal_complete(end: dict | None, attempts: dict[str, list[dict]]) -> bool:
    if end is None or end["scheduled_writes"] != len(attempts):
        return False
    return all(any(t["final"] for t in tries) for tries in attempts.values())


def write_gap(records: list[dict], *, load_end_s: float) -> dict:
    """성공한 쓰기 응답 사이의 최대 간격. 503은 쓰기가 멈췄다는 신호라 간격을 끊지 않는다.

    부하 시작부터 첫 성공, 마지막 성공부터 부하 끝까지도 간격으로 본다. 그래야 끝까지 멈춘 경우를 놓치지 않는다.
    부하가 끝난 뒤의 재시도 응답은 세지 않는다. 쓰기 공백은 부하를 넣는 동안의 값이다.
    """
    if load_end_s <= 0:
        raise ValueError("load_end_s must be positive")
    acks = sorted(
        r["finished_at_s"]
        for r in records
        if r.get("type", "attempt") == "attempt"
        and r["kind"] == "write"
        and r["acked"]
        and r["finished_at_s"] <= load_end_s
    )
    if not acks:
        return {
            "max_ms": load_end_s * 1000,
            "from_s": 0.0,
            "to_s": load_end_s,
            "position": "no_ack",
            "acks": 0,
        }
    points = [0.0, *acks]
    if acks[-1] < load_end_s:
        points.append(load_end_s)
    start, finish = max(zip(points, points[1:], strict=False), key=lambda p: p[1] - p[0])
    if start == 0.0:
        position = "lead"
    elif finish > acks[-1]:
        position = "tail"
    else:
        position = "between"
    return {
        "max_ms": round((finish - start) * 1000, 3),
        "from_s": start,
        "to_s": finish,
        "position": position,
        "acks": len(acks),
    }
