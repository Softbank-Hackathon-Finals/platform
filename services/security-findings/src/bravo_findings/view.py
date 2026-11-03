"""한 화면 데이터: 수집 상태 판정, 환경 × 범주 요약, 탐지 능력 표 (설계 6 · 7절)."""

from bravo_findings.rules import Rules
from bravo_findings.store import SourceRow, Store

CATEGORIES = ["IAM", "ENCRYPTION", "AUDIT", "EXPOSURE", "POLICY", "THREAT", "VULNERABILITY"]
# 칸 상태는 나쁜 쪽이 이긴다: 확인 불가 > 오래됨 > 정상
_WORST = {"FAILED": 3, "STALE": 2, "OK": 1}


def source_state(row: SourceRow, now: int, stale_ms: int) -> str:
    """OK · FAILED(확인 불가) · STALE(오래됨) · DISABLED(없음). 수집 실패를 0건으로 보지 않는다."""
    if not row.enabled:
        return "DISABLED"
    if row.error_code and (
        row.last_success_time is None or (row.last_attempt_time or 0) >= row.last_success_time
    ):
        return "FAILED"
    if row.last_success_time is None or not 0 <= now - row.last_success_time <= stale_ms:
        return "STALE"
    return "OK"


def source_status_doc(row: SourceRow, now: int, stale_ms: int) -> dict:
    """finding.schema.json의 SourceStatus 형식."""
    state = source_state(row, now, stale_ms)
    doc = {
        "kind": "source_status",
        "source": row.source,
        "environment_id": row.environment_id,
        "state": state,
        "checked_time": now,
    }
    if row.last_success_time is not None:
        doc["last_success_time"] = row.last_success_time
    if state == "FAILED":
        doc["error_code"] = row.error_code
    elif state == "DISABLED":
        doc["error_code"] = "NOT_ENABLED"
    if row.cursor:
        doc["cursor"] = row.cursor
    return doc


class View:
    def __init__(self, store: Store, rules: Rules):
        self.store, self.rules = store, rules
        self.stale_ms = rules.mappings["source_status"]["stale_after_minutes"] * 60_000

    def _sources_by_env(self, allowed=None) -> dict[str, list[SourceRow]]:
        out: dict[str, list[SourceRow]] = {}
        for row in self.store.sources():
            if allowed is not None and row.environment_id not in allowed:
                continue
            out.setdefault(row.environment_id, []).append(row)
        return out

    def statuses(self, now: int, allowed=None) -> list[dict]:
        return [
            source_status_doc(r, now, self.stale_ms)
            for r in self.store.sources()
            if allowed is None or r.environment_id in allowed
        ]

    def summary(self, now: int, allowed=None) -> dict:
        coverage = self.rules.mappings["coverage"]
        counts = self.store.open_counts()
        environments = []
        for env, rows in self._sources_by_env(allowed).items():
            cells = {}
            for category in CATEGORIES:
                covering = [r for r in rows if category in coverage.get(r.source, [])]
                active = [(r, source_state(r, now, self.stale_ms)) for r in covering]
                active = [(r, s) for r, s in active if s != "DISABLED"]
                if not active:
                    cells[category] = {"state": "NONE", "counts": {}, "sources": []}
                    continue
                worst = max((s for _, s in active), key=_WORST.__getitem__)
                successes = [
                    r.last_success_time for r, _ in active if r.last_success_time is not None
                ]
                cell = {
                    "state": {"OK": "OK", "STALE": "STALE", "FAILED": "UNKNOWN"}[worst],
                    "counts": {
                        str(k): v for k, v in sorted(counts.get((env, category), {}).items())
                    },
                    "sources": sorted(r.source for r, _ in active),
                }
                if worst != "OK" and successes:
                    cell["last_success_time"] = min(successes)
                cells[category] = cell
            environments.append({"environment_id": env, "cells": cells})
        return {"generated_time": now, "categories": CATEGORIES, "environments": environments}

    def capabilities(self, now: int, allowed=None) -> dict:
        rows_spec = self.rules.mappings["capabilities"]["rows"]
        environments = []
        for env, rows in self._sources_by_env(allowed).items():
            caps = {}
            for spec in rows_spec:
                present = [r for r in rows if r.source in spec["sources"] and r.enabled]
                caps[spec["id"]] = {
                    "available": bool(present),
                    "sources": sorted(r.source for r in present),
                    "states": {r.source: source_state(r, now, self.stale_ms) for r in present},
                }
            environments.append({"environment_id": env, "capabilities": caps})
        return {
            "generated_time": now,
            "rows": [{"id": s["id"], "name": s["name"]} for s in rows_spec],
            "environments": environments,
        }
