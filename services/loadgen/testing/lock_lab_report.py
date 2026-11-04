#!/usr/bin/env python3
"""lock_lab.py matrix 결과(JSONL)를 README에 붙일 표로 바꾼다. 표의 숫자를 사람이 옮겨 적지 않기 위해서다.

    uv run python testing/lock_lab_report.py <결과.jsonl>... [--names 안 ...] [--pause | --boundary]
    uv run python testing/lock_lab_report.py <파일.json> (--wire | --ceiling | --invariants)
    uv run python testing/lock_lab_report.py ceiling.json calibration.json wire.json --prediction

- 칸마다 회차를 모두 적는다(`회차1 / 회차2`). 한쪽만 나쁜 칸을 평균으로 감추지 않는다.
- 5xx는 쓰기와 읽기를 더한 수다. 실패 · 유실 · 중복이 0이 아닌 칸만 따로 적는다.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

LABELS = {
    "baseline": "원본",
    "select-after-commit": "안 1",
    "cte-autocommit": "안 2",
    "pipeline": "안 3",
    "pipeline-begin": "안 3(BEGIN)",
    "cte-autocommit-reads": "안 2+읽기",
    "pipeline-reads": "안 3+읽기",
}


def verdict(row: dict) -> str:
    judged = row["judge"]
    if "FAIL" in judged:
        return "FAIL"
    # 부하가 끝난 뒤 남은 재시도만 담긴 마지막 창은 표본이 모자라 UNKNOWN이 된다. 부하 구간의 판정이 아니다.
    body = judged[:-1] if judged[-1] == "UNKNOWN" else judged
    return "PASS" if all(v == "PASS" for v in body) else "UNKNOWN"


def seconds_or_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{value:.0f}ms"


def cell_text(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["rep"])
    verdicts = {verdict(r) for r in rows}
    head = verdicts.pop() if len(verdicts) == 1 else " / ".join(verdict(r) for r in rows)
    errors = " / ".join(f'{r["write"]["http_5xx"] + r["read"]["http_5xx"]:,}' for r in rows)
    p95 = " / ".join(seconds_or_ms(r["write"]["p95_ms"]) for r in rows)
    return f"{head}<br>5xx {errors}<br>p95 {p95}"


def matrix_table(rows: list[dict], names: list[str]) -> str:
    cells = defaultdict(list)
    for row in rows:
        cells[(row["rtt_ms"], row["target_percent"], row["variant"])].append(row)
    keys = sorted({(r["rtt_ms"], r["target_percent"]) for r in rows})
    lines = ["| 설정 왕복 | 실제 왕복(부하 중) | 목적지 | " + " | ".join(LABELS.get(n, n) for n in names) + " |"]
    lines.append("|---:|---:|---:|" + "---|" * len(names))
    for rtt, percent in keys:
        measured = [r["effective_rtt_ms"]["p50_ms"] for r in rows if (r["rtt_ms"], r["target_percent"]) == (rtt, percent) and r.get("effective_rtt_ms")]
        real = f"{min(measured):.1f}~{max(measured):.1f}ms" if measured else "-"
        columns = [cell_text(cells[(rtt, percent, name)]) if cells.get((rtt, percent, name)) else "-" for name in names]
        lines.append(f"| {rtt:g}ms | {real} | {percent}% | " + " | ".join(columns) + " |")
    return "\n".join(lines)


def pause_table(rows: list[dict], names: list[str]) -> str:
    lines = ["| 설정 왕복 | 목적지 | 안 | 출발지 PAUSE | 목적지 PAUSE | judge |", "|---:|---:|---|---:|---:|---|"]
    for row in sorted(rows, key=lambda r: (r["rtt_ms"], names.index(r["variant"]) if r["variant"] in names else 99, r["rep"])):
        if row["variant"] not in names or not row.get("pause_ms"):
            continue
        pause = row["pause_ms"]
        lines.append(
            f'| {row["rtt_ms"]:g}ms | {row["target_percent"]}% | {LABELS.get(row["variant"], row["variant"])} | '
            f'{pause.get("source", 0):,.1f}ms | {pause.get("target", 0):,.1f}ms | {verdict(row)} |'
        )
    return "\n".join(lines)


def stalled(row: dict) -> bool:
    """지연이 없는 출발지까지 응답이 늦고 시간 초과가 났는데 앱은 오류를 남기지 않은 칸. 원인은 확인하지 못했다."""
    return row["unattributed"]["timeout"] > 0 and row["pools"]["source"]["p95_ms_worst_window"] > 1000 and not row["app_errors"]["source"]


def edge_made(row: dict) -> bool:
    """앱이 답하지 않고 Edge 대역이 직접 만든 응답이 있는 칸. 앱의 결과가 아니라 대역의 동작이다."""
    return row["unattributed"]["other"] > 0


def mark(row: dict) -> str:
    return "S" if stalled(row) else "E" if edge_made(row) else verdict(row)[0]


def boundary_table(rows: list[dict], names: list[str]) -> str:
    """원인 미확인 멈춤(S)과 Edge 대역의 502(E)가 낀 회차를 빼고, 모두 PASS인 가장 큰 왕복과 과반이 FAIL인 가장 작은 왕복을 찾는다."""
    lines = ["| 안 | 목적지 | 마지막 PASS(실제 왕복) | 처음 FAIL(실제 왕복) | 회차별 결과 |", "|---|---:|---:|---:|---|"]
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["variant"], row["target_percent"], row["rtt_ms"])].append(row)

    def real(found: list[dict]) -> str:
        return f'{sum(r["effective_rtt_ms"]["p50_ms"] for r in found) / len(found):.1f}ms'

    for name in names:
        for percent in sorted({r["target_percent"] for r in rows}):
            rtts = sorted(rtt for variant, pct, rtt in grouped if variant == name and pct == percent)
            if not rtts:
                continue
            valid = {rtt: [r for r in grouped[(name, percent, rtt)] if mark(r) not in "SE"] for rtt in rtts}
            failing = [rtt for rtt in rtts if valid[rtt] and sum(verdict(r) == "FAIL" for r in valid[rtt]) * 2 > len(valid[rtt])]
            first_fail = failing[0] if failing else None
            passing = [rtt for rtt in rtts if valid[rtt] and all(verdict(r) == "PASS" for r in valid[rtt]) and (first_fail is None or rtt < first_fail)]
            marks = []
            for rtt in rtts:
                found = sorted(grouped[(name, percent, rtt)], key=lambda r: r["rep"])
                marks.append(f"{rtt:g}ms " + "".join(mark(r) for r in found))
            lines.append(
                f"| {LABELS.get(name, name)} | {percent}% | "
                f"{f'{max(passing):g}ms({real(valid[max(passing)])})' if passing else '-'} | "
                f"{f'{first_fail:g}ms({real(valid[first_fail])})' if failing else '없음'} | {' · '.join(marks)} |"
            )
    return "\n".join(lines)


def nonzero_nfr2(rows: list[dict]) -> str:
    found = [r for r in rows if any(r["nfr2"].values()) or r["content_mismatch"] or r["phantom_rows"]]
    if not found:
        return "모든 칸에서 실패 · 유실 · 중복 · 내용 불일치 · 유령 행 0."
    lines = ["| 안 | 설정 왕복 | 목적지 | 회차 | 실패 | 유실 | 중복 | 내용 불일치 | 유령 행 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in sorted(found, key=lambda r: (r["variant"], r["rtt_ms"], r["target_percent"], r["rep"])):
        n = r["nfr2"]
        lines.append(
            f'| {LABELS.get(r["variant"], r["variant"])} | {r["rtt_ms"]:g}ms | {r["target_percent"]}% | {r["rep"]} | '
            f'{n["failed"]} | {n["lost"]} | {n["duplicated"]} | {r["content_mismatch"]} | {r["phantom_rows"]} |'
        )
    return "\n".join(lines)


def wire_table(variants: dict) -> str:
    lines = ["| 안 | 새 쓰기 | 재전송 | 충돌 | 없는 첨부 | 새 쓰기의 잠금 점유 |", "|---|---:|---:|---:|---:|---:|"]
    for name, ops in variants.items():
        columns = [f'{ops[op]["round_trips"]} / {ops[op]["hold_round_trips"]}' for op in ("fresh", "replay", "conflict", "unknown_attachment")]
        lines.append(f'| {LABELS.get(name, name)} | ' + " | ".join(columns) + f' | {ops["fresh"]["hold_ms"]:.2f}ms |')
    return "\n".join(lines)


def ceiling_table(rows: list[dict], names: list[str], tx_timeout_ms: int = 5000) -> str:
    rtts = sorted({r["rtt_ms"] for r in rows})
    lines = ["| 안 | " + " | ".join(f"설정 {rtt:g}ms" for rtt in rtts) + " |", "|---|" + "---:|" * len(rtts)]
    for name in names:
        found = {r["rtt_ms"]: r for r in rows if r["variant"] == name and r["tx_timeout_ms"] == tx_timeout_ms}
        lines.append(f"| {LABELS.get(name, name)} | " + " | ".join(f'{found[rtt]["tx_per_s"]:,.1f}' if rtt in found else "-" for rtt in rtts) + " |")
    return "\n".join(lines)


def prediction_table(ceiling: list[dict], calibration: list[dict], wire: dict, tx_timeout_ms: int = 5000) -> str:
    """처리량이 예측으로 설명되는지 본다. 잠금이 한계면 1 / (점유 왕복 × 왕복 + s), 풀이 한계면 10 / (전체 왕복 × 왕복 + s)다. s는 설정 0ms 칸에서 구한다."""
    real = {r["nominal_rtt_ms"]: r["p50_ms"] for r in calibration if r["path"] == "proxy"}
    rows = {(r["variant"], r["rtt_ms"]): r["tx_per_s"] for r in ceiling if r["tx_timeout_ms"] == tx_timeout_ms}
    rtts = sorted({rtt for _, rtt in rows})
    names = [name for name in LABELS if (name, rtts[0]) in rows and name in wire]
    lines = ["| 안 | 한계 | " + " | ".join(f"설정 {rtt:g}ms" for rtt in rtts[1:]) + " |", "|---|---|" + "---:|" * len(rtts[1:])]
    for name in names:
        hold, total = wire[name]["fresh"]["hold_round_trips"], wire[name]["fresh"]["round_trips"]
        slots, trips, limit = (1, hold, f"잠금: 1 / ({hold}왕복 + s)") if hold else (10, total, f"풀: 10 / ({total}왕복 + s)")
        service = slots / rows[(name, rtts[0])] - trips * real[rtts[0]] / 1000
        columns = []
        for rtt in rtts[1:]:
            predicted = slots / (trips * real[rtt] / 1000 + service)
            columns.append(f"{rows[(name, rtt)]:,.1f} / {predicted:,.1f} ({rows[(name, rtt)] / predicted:.2f})")
        lines.append(f"| {LABELS.get(name, name)} | {limit} | " + " | ".join(columns) + " |")
    return "\n".join(lines)


def invariants_table(runs: list[dict]) -> str:
    names = list(dict.fromkeys(r["variant"] for r in runs))
    lines = ["| 안 | 조합 | 검사 통과 | 뒤집힘(최대) | 건너뜀(최대) | 커밋 뒤 응답 유실(최소) | demo-chat 자체 시험 |", "|---|---:|---:|---:|---:|---:|---|"]
    for name in sorted(names, key=lambda n: (list(LABELS).index(n) if n in LABELS else 99, n)):
        mine = [r for r in runs if r["variant"] == name]
        checks = [c for r in mine for c in r["checks"]]
        inversions = max(c["actual"] for c in checks if "뒤집힘" in c["check"])
        skipped = max(max(c["actual"]) for c in checks if "건너뛴" in c["check"])
        losses = [c["actual"] for c in checks if c["invariant"] == "connection-loss" and "응답 유실이 실제로" in c["check"]]
        unit = {r["demo_chat_pytest"]["summary"].split(",")[0] for r in mine if r.get("demo_chat_pytest")}
        lines.append(
            f'| {LABELS.get(name, name)} | {len(mine)} | {sum(c["ok"] for c in checks)}/{len(checks)} | {inversions} | {skipped} | '
            f'{min(losses) if losses else "-"} | {", ".join(sorted(unit))} |'
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--names", nargs="+", default=list(LABELS))
    parser.add_argument("--pause", action="store_true", help="PAUSE 표만 낸다")
    parser.add_argument("--boundary", action="store_true", help="경계 표만 낸다")
    parser.add_argument("--wire", action="store_true", help="wire.json의 왕복 수 표를 낸다")
    parser.add_argument("--ceiling", action="store_true", help="ceiling.json의 처리량 표를 낸다")
    parser.add_argument("--invariants", action="store_true", help="invariants.json의 불변식 표를 낸다")
    parser.add_argument("--prediction", action="store_true", help="ceiling.json calibration.json wire.json으로 예측과 실측을 견준다")
    args = parser.parse_args()
    if args.prediction:
        ceiling, calibration, wire = (json.loads(path.read_text()) for path in args.paths)
        print(prediction_table(ceiling["rows"], calibration["rows"], wire["variants"]))
        return 0
    if args.wire or args.ceiling or args.invariants:
        data = json.loads(args.paths[0].read_text())
        if args.wire:
            print(wire_table(data["variants"]))
        elif args.ceiling:
            print(ceiling_table(data["rows"], [n for n in args.names if any(r["variant"] == n for r in data["rows"])]))
        else:
            print(invariants_table(data["runs"]))
        return 0
    rows = [json.loads(line) for path in args.paths for line in path.read_text().splitlines() if line]
    names = [name for name in args.names if any(r["variant"] == name for r in rows)]
    if args.pause:
        print(pause_table(rows, names))
    elif args.boundary:
        print(boundary_table(rows, names))
    else:
        print(matrix_table(rows, names))
        print()
        print(nonzero_nfr2(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
