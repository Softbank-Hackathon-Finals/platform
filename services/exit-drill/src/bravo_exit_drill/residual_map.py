"""잔존 의존성 지도. 이전 뒤에도 무엇이 남는지를 등급과 함께 보여 준다(설계 FR-15, 8.11).

인벤토리는 이전 전에 찍은 사진이라 그것만으로 "이제 없다"고 말할 수 없다. 대체 구현이 있어도
Exit Drill을 통과하기 전에는 '확인 안 됨'이고, 항목은 어떤 경우에도 지도에서 빠지지 않는다.

통과해도 "AWS 경로가 막혔고 앱이 떠서 응답했다"까지만 말한다. 첨부 읽기나 OpenBao 호출처럼
기능별로 대체 구현을 쓰는지는 이 시험이 재지 않는다.
"""

from .inventory_input import Dependency, InventoryView, grade_label

MAP_VERSION = "exit-drill-residual-map/v1"
NOT_GRADED = "not_graded"
NOT_RUN = "NOT_RUN"

# 먼저 봐야 할 것부터 적는다. 보고서의 구역 순서이기도 하다.
STATUS_LABELS = {
    "remains": "남음",
    "broken": "끊김 (남겨야 할 복제 채널)",
    "unknown": "불명 (판정 못 함)",
    "kept": "유지 (복제 채널)",
    "not_blocked": "차단 대상 아님 (이 시험으로 확인 안 됨)",
    "unverified": "대체 있음 · 아직 확인 안 됨",
    "blocked_app_ok": "AWS 경로 막힘 · 앱 동작 확인",
}
GRADE_LABELS = {NOT_GRADED: "등급 없음"}


class MapError(ValueError):
    """입력들이 같은 시험의 것이 아니다."""


def build_map(inventory: InventoryView, plan: dict, report: dict | None) -> dict:
    if inventory.digest != plan["inventory_digest"]:
        raise MapError("inventory does not belong to the plan")
    if inventory.candidate != plan["candidate"]:
        raise MapError("candidate differs from the plan")
    if report is not None and not isinstance(report, dict):
        raise MapError("report is malformed")
    if report is not None and report.get("plan_digest") != plan["digest"]:
        raise MapError("report does not belong to the plan")
    verdict, results = _read_report(report)
    entries = [
        *[_dependency_entry(d, plan, results, verdict) for d in inventory.dependencies],
        *[_channel_entry(d, results) for d in plan["decisions"] if d["action"] == "KEEP"],
        *[_workload_entry(name, w) for name, w in sorted(plan["workloads"].items()) if w["role"] == "infrastructure"],
        *_residual_entries(inventory, plan),
    ]
    return {
        "schema_version": MAP_VERSION,
        "app": inventory.app_id,
        "candidate": inventory.candidate,
        "candidate_verdict": inventory.candidate_verdict,
        "candidate_missing_capabilities": list(inventory.missing_capabilities),
        "inventory_trace_notes": list(inventory.trace_notes),
        "inventory_digest": inventory.digest,
        "plan_digest": plan["digest"],
        "drill_verdict": verdict,
        "drill_not_pass": _not_pass(report),
        "summary": {"status": _count(entries, "status"), "grade": _count(entries, "grade")},
        "entries": entries,
    }


def _read_report(report: dict | None) -> tuple[str, dict[str, str]]:
    if report is None:
        return NOT_RUN, {}
    try:
        return report["verdict"], {c["id"]: c["verdict"] for c in report["checks"]}
    except (KeyError, TypeError) as exc:
        raise MapError("report is malformed") from exc


def _not_pass(report: dict | None) -> list[dict]:
    """판정이 통과하지 못한 이유. 호환 구성의 비자격도 여기 나온다. 지도만 읽어도 왜인지 알게 한다."""
    if report is None:
        return []
    return [{"id": c["id"], "verdict": c["verdict"], "code": c.get("code")} for c in report["checks"] if c["verdict"] != "PASS"]


def _count(entries: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry[field]] = counts.get(entry[field], 0) + 1
    return dict(sorted(counts.items()))


def _entry(**fields) -> dict:
    return {
        "role": "", "grade": NOT_GRADED, "grade_by": "", "target_impl": "", "risk": "", "found_in": "",
        "evidence": [], "source": "", "check_verdict": NOT_RUN, **fields,
    }


def _block_checks(role: str, plan: dict) -> list[str]:
    return sorted(
        f"block:{d['route_id']}:{owner}"
        for d in plan["decisions"]
        if d["action"] == "BLOCK" and role in d["roles"]
        for owner in d["owners"]
    )


def _combined(check_ids: list[str], results: dict[str, str]) -> str:
    values = [results.get(check_id, NOT_RUN) for check_id in check_ids]
    if not values or NOT_RUN in values:
        return NOT_RUN
    if "FAIL" in values:
        return "FAIL"
    return "PASS" if all(v == "PASS" for v in values) else "UNKNOWN"


def _dependency_status(dependency: Dependency, check_ids: list[str], combined: str, drill_verdict: str) -> str:
    if dependency.replacement == "none":
        return "remains"
    if dependency.replacement != "replaceable" or not dependency.target_impl or not check_ids:
        return "unknown"
    if dependency.awaiting_review:
        # 차단 시험에 안 걸렸어도 에이전트 확인을 기다리는 항목은 확정하지 않는다.
        return "unknown"
    if combined == "FAIL":
        return "remains"
    return "blocked_app_ok" if combined == "PASS" and drill_verdict == "PASS" else "unverified"


def _dependency_entry(dependency: Dependency, plan: dict, results: dict[str, str], drill_verdict: str) -> dict:
    check_ids = _block_checks(dependency.role, plan)
    combined = _combined(check_ids, results)
    return _entry(
        id=dependency.id,
        category="source_dependency",
        name=f"{dependency.role} ({', '.join(dependency.services)})",
        role=dependency.role,
        grade=dependency.grade,
        grade_by=dependency.grade_by,
        target_impl=dependency.target_impl,
        risk=dependency.risk,
        found_in=dependency.found_in,
        status=_dependency_status(dependency, check_ids, combined, drill_verdict),
        evidence=check_ids,
        source="inventory",
        check_verdict=combined,
    )


def _channel_entry(decision: dict, results: dict[str, str]) -> dict:
    """복제를 남긴 만큼 출발지와 아직 이어져 있다. 그 사실을 지도에 드러낸다."""
    route_id = decision["route_id"]
    suffixes = ["stream"] if decision["evidence"] == "stream" else decision["owners"]
    check_ids = [f"keep:{route_id}:{suffix}" for suffix in suffixes]
    combined = _combined(check_ids, results)
    return _entry(
        id=f"channel:{route_id}",
        category="replication_channel",
        name=f"복제 채널 {route_id} (Tailscale로 출발지 DB와 연결)",
        status="broken" if combined == "FAIL" else "kept",
        evidence=check_ids,
        source="drill-plan",
        check_verdict=combined,
    )


def _workload_entry(name: str, workload: dict) -> dict:
    """정책을 걸지 않은 구성요소. 이것을 거쳐 AWS에 닿는 의존은 이 시험으로 잡지 못한다."""
    return _entry(
        id=f"workload:{name}",
        category="unblocked_workload",
        name=_selector_label(name, workload),
        status="not_blocked",
        source="drill-plan",
    )


def _residual_entries(inventory: InventoryView, plan: dict) -> list[dict]:
    """T16 계약의 잔존 의존성은 등급이 없는 글자다. 등급을 지어내지 않고 '등급 없음'으로 둔다."""
    sources = {name: "drill-spec" for name in plan["declared_residuals"]}
    sources.update({name: "inventory" for name in inventory.residuals})
    # 목적지에서 그 서비스에 기대는 구성요소(Cloudflare Tunnel connector 등). 막지 않으므로 남음 그대로다.
    edges: dict[str, list[str]] = {}
    for name, workload in sorted(plan["workloads"].items()):
        if workload["role"] == "edge":
            edges.setdefault(workload["residual"], []).append(_selector_label(name, workload))
    return [
        _entry(
            id=f"residual:{name}", category="external_service", name=name, status="remains", source=source,
            target_impl=", ".join(edges.get(name, [])),
        )
        for name, source in sorted(sources.items())
    ]


def _selector_label(name: str, workload: dict) -> str:
    return f"{name} ({', '.join(f'{k}={v}' for k, v in sorted(workload['selector'].items()))})"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ") if value not in ("", None) else "-"


def render_markdown(result: dict) -> str:
    lines = [
        f"# 잔존 의존성 지도: {_cell(result['app'])} → {_cell(result['candidate'])}",
        "",
        f"- Exit Drill 판정: **{_cell(result['drill_verdict'])}**",
        f"- 후보 적합성(T16): {_cell(result['candidate_verdict'])}",
        *[f"- 목적지에 없는 기능(T16): {_cell(gap)}" for gap in result["candidate_missing_capabilities"]],
        f"- 인벤토리: `{_cell(result['inventory_digest'])}`",
        f"- 차단 계획: `{_cell(result['plan_digest'])}`",
        "",
    ]
    if result["inventory_trace_notes"]:
        # 못 읽은 입력이 있으면 아래 모든 항목의 근거가 그만큼 좁다. 그래서 맨 위에 둔다.
        lines += [
            f"## 인벤토리 추적 메모 ({len(result['inventory_trace_notes'])})",
            "",
            "T16이 배포를 따라가며 남긴 메모다. 읽지 못한 입력도 여기 나온다.",
            "",
            *[f"- {_cell(note)}" for note in result["inventory_trace_notes"]],
            "",
        ]
    if result["drill_not_pass"]:
        lines += [
            f"## 판정을 막은 항목 ({len(result['drill_not_pass'])})",
            "",
            "| 항목 | 판정 | 코드 |",
            "| --- | --- | --- |",
            *[f"| {_cell(c['id'])} | {_cell(c['verdict'])} | {_cell(c['code'])} |" for c in result["drill_not_pass"]],
            "",
        ]
    for status, label in STATUS_LABELS.items():
        rows = [e for e in result["entries"] if e["status"] == status]
        if not rows:
            continue
        lines += [
            f"## {label} ({len(rows)})",
            "",
            "| 항목 | 등급 | 목적지 구현 | 위험 | 시험 | 근거 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for entry in rows:
            grade = GRADE_LABELS.get(entry["grade"]) or grade_label(entry["grade"])
            lines.append(
                f"| {_cell(entry['name'])} | {_cell(grade)} | {_cell(entry['target_impl'])} | {_cell(entry['risk'])} "
                f"| {_cell(entry['check_verdict'])} | {_cell(', '.join(entry['evidence']))} |"
            )
        lines.append("")
    return "\n".join(lines)
