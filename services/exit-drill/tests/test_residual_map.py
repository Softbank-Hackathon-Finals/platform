"""잔존 의존성 지도. 인벤토리 항목을 빼지 않고, 증거만큼만 '독립'이라고 말한다."""

import pytest
from helpers import inventory_doc, redigest, snapshot_doc, spec_doc

from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.policy import build_plan
from bravo_exit_drill.residual_map import MapError, build_map, render_markdown
from bravo_exit_drill.spec import parse_spec


def plan_and_view(doc=None):
    doc = inventory_doc() if doc is None else doc
    view = parse_inventory(doc, candidate="onprem-demo")
    plan = build_plan(parse_spec(spec_doc()), view, parse_snapshot(snapshot_doc(), namespace="target-dev"))
    return plan, view


def report_for(plan: dict, verdict: str = "PASS", overrides: dict | None = None) -> dict:
    """모든 차단 · 유지 항목이 통과한 판정 결과를 만들고 필요한 곳만 바꾼다."""
    checks = []
    for decision in plan["decisions"]:
        if decision["action"] == "BLOCK":
            checks.extend({"id": f"block:{decision['route_id']}:{o}", "verdict": "PASS", "code": "DENIED"} for o in decision["owners"])
        elif decision["action"] == "KEEP":
            suffix = "stream" if decision["evidence"] == "stream" else decision["owners"][0]
            checks.append({"id": f"keep:{decision['route_id']}:{suffix}", "verdict": "PASS", "code": "OPEN"})
    for check in checks:
        check.update((overrides or {}).get(check["id"], {}))
    return {"schema_version": "exit-drill-report/v1", "verdict": verdict, "plan_digest": plan["digest"], "checks": checks}


def entries(result: dict) -> dict:
    return {e["id"]: e for e in result["entries"]}


def test_every_inventory_item_appears_with_its_grade():
    plan, view = plan_and_view()
    got = entries(build_map(view, plan, None))
    for item in inventory_doc()["items"]:
        assert got[item["id"]]["grade"] == item["grade"]
        assert got[item["id"]]["grade_by"] == item["grade_by"]
        assert got[item["id"]]["role"] == item["role"]
        assert got[item["id"]]["target_impl"] == item["target"]["impl"]


def test_without_drill_result_nothing_is_called_independent():
    plan, view = plan_and_view()
    result = build_map(view, plan, None)
    assert result["drill_verdict"] == "NOT_RUN"
    assert "blocked_app_ok" not in {e["status"] for e in result["entries"]}
    assert entries(result)["item-004"]["status"] == "unverified"


def test_passed_drill_marks_replaced_items_as_blocked_with_app_working():
    plan, view = plan_and_view()
    got = entries(build_map(view, plan, report_for(plan)))
    assert got["item-004"]["status"] == "blocked_app_ok"
    assert got["item-004"]["evidence"] == ["block:aws-s3:app"]
    assert got["item-001"]["status"] == "blocked_app_ok"  # db는 출발지 풀러 경로 차단으로 잰다
    assert got["item-001"]["evidence"] == ["block:pooler-to-source:pooler"]


def test_iam_needs_all_of_its_routes():
    plan, view = plan_and_view()
    report = report_for(plan, verdict="UNKNOWN", overrides={"block:aws-sts:app": {"verdict": "UNKNOWN", "code": "NOT_OBSERVED"}})
    assert entries(build_map(view, plan, report))["item-002"]["status"] == "unverified"


def test_open_aws_path_means_the_dependency_remains():
    plan, view = plan_and_view()
    report = report_for(plan, verdict="FAIL", overrides={"block:aws-s3:app": {"verdict": "FAIL", "code": "SOURCE_PATH_OPEN"}})
    got = entries(build_map(view, plan, report))
    assert got["item-004"]["status"] == "remains"
    assert got["item-005"]["status"] == "unverified"  # 드릴 전체가 실패했으면 다른 항목도 독립이라고 못 한다


def test_route_passed_but_drill_failed_is_not_confirmed():
    """막혔어도 앱이 안 뜨면 그 의존성 없이 동작한다고 말할 수 없다."""
    plan, view = plan_and_view()
    assert entries(build_map(view, plan, report_for(plan, verdict="FAIL")))["item-004"]["status"] == "unverified"


def test_grade_none_remains_even_when_drill_passed():
    doc = inventory_doc()
    doc["items"][3]["grade"] = "none"
    doc["items"][3]["target"]["impl"] = ""
    plan, view = plan_and_view(redigest(doc))
    assert entries(build_map(view, plan, report_for(plan)))["item-004"]["status"] == "remains"


def test_grade_unknown_stays_unknown_even_when_drill_passed():
    doc = inventory_doc()
    doc["items"][3]["grade"] = "unknown"
    plan, view = plan_and_view(redigest(doc))
    assert entries(build_map(view, plan, report_for(plan)))["item-004"]["status"] == "unknown"


def test_new_grade_value_stays_visible_as_unknown():
    doc = inventory_doc()
    doc["items"][3]["grade"] = "rewrite"
    plan, view = plan_and_view(redigest(doc))
    got = entries(build_map(view, plan, report_for(plan)))["item-004"]
    assert (got["grade"], got["status"]) == ("rewrite", "unknown")


def test_rule_grade_waiting_for_agent_stays_unknown():
    """T16 고정 입력의 kms는 배포에만 있어 에이전트 확인을 기다린다. 통과해도 확정하지 않는다."""
    plan, view = plan_and_view()
    got = entries(build_map(view, plan, report_for(plan)))["item-003"]
    assert got["status"] == "unknown"
    assert got["risk"] == "migration_omission"


def test_agent_graded_item_follows_the_evidence():
    doc = inventory_doc()
    doc["items"][2]["grade_by"] = "agent"
    doc["items"][2]["rationale"] = "코드에서 쓰지 않음을 확인했다"
    plan, view = plan_and_view(redigest(doc))
    assert entries(build_map(view, plan, report_for(plan)))["item-003"]["status"] == "blocked_app_ok"


def test_no_replacement_target_is_unknown():
    doc = inventory_doc()
    doc["items"][3]["target"]["impl"] = ""
    plan, view = plan_and_view(redigest(doc))
    assert entries(build_map(view, plan, report_for(plan)))["item-004"]["status"] == "unknown"


def test_unmapped_dependency_is_listed_as_unknown():
    doc = inventory_doc()
    doc["items"].append({
        **doc["items"][0], "id": "item-006", "role": "unknown", "service": "google.cloud",
        "grade": "unknown", "agent_needed": True,
    })
    plan, view = plan_and_view(redigest(doc))
    assert entries(build_map(view, plan, None))["item-006"]["status"] == "unknown"


def test_replication_channels_are_shown_as_kept():
    """복제를 남겼으므로 출발지와의 연결이 아직 있다는 사실을 지도에 드러낸다."""
    plan, view = plan_and_view()
    got = entries(build_map(view, plan, report_for(plan)))
    channel = got["channel:replication-forward"]
    assert channel["status"] == "kept"
    assert channel["category"] == "replication_channel"
    assert channel["grade"] == "not_graded"
    assert channel["evidence"] == ["keep:replication-forward:db"]
    assert got["channel:replication-reverse"]["evidence"] == ["keep:replication-reverse:stream"]


def test_broken_channel_is_not_shown_as_kept():
    plan, view = plan_and_view()
    report = report_for(plan, verdict="FAIL", overrides={"keep:replication-forward:db": {"verdict": "FAIL", "code": "REPLICATION_PATH_BROKEN"}})
    got = entries(build_map(view, plan, report))
    assert got["channel:replication-forward"]["status"] == "broken"
    assert got["channel:replication-reverse"]["status"] == "kept"


def test_unrecognized_check_result_is_not_a_pass():
    plan, view = plan_and_view()
    report = report_for(plan, overrides={"block:aws-s3:app": {"verdict": "SKIPPED", "code": "X"}})
    got = entries(build_map(view, plan, report))["item-004"]
    assert got["check_verdict"] == "UNKNOWN"
    assert got["status"] == "unverified"


def test_workload_the_drill_did_not_block_is_listed():
    """차단하지 않은 구성요소를 거친 AWS 의존은 이 시험으로 잡지 못한다. 그 사실을 드러낸다."""
    plan, view = plan_and_view()
    entry = entries(build_map(view, plan, report_for(plan)))["workload:objects"]
    assert entry["status"] == "not_blocked"
    assert entry["category"] == "unblocked_workload"


def test_residual_services_remain_with_source_recorded():
    plan, view = plan_and_view()
    got = entries(build_map(view, plan, None))
    cloudflare = got["residual:cloudflare (edge · tunnel)"]
    assert (cloudflare["status"], cloudflare["source"], cloudflare["grade"]) == ("remains", "inventory", "not_graded")
    assert got["residual:tailscale (복제 · 제어 경로)"]["source"] == "drill-spec"


def test_duplicate_residual_is_listed_once():
    doc = inventory_doc()
    doc["candidates"][0]["residual_dependencies"].append("tailscale (복제 · 제어 경로)")
    plan, view = plan_and_view(redigest(doc))
    assert [e["id"] for e in build_map(view, plan, None)["entries"]].count("residual:tailscale (복제 · 제어 경로)") == 1


def test_summary_counts_everything_listed():
    plan, view = plan_and_view()
    result = build_map(view, plan, report_for(plan))
    assert sum(result["summary"]["status"].values()) == len(result["entries"])
    assert result["summary"]["grade"]["code_change"] == 2
    assert result["summary"]["grade"]["not_graded"] == 8  # 복제 채널 2 + 인벤토리 2 + 명세 3 + 차단 안 한 워크로드 1


def test_map_binds_inputs():
    plan, view = plan_and_view()
    result = build_map(view, plan, report_for(plan))
    assert result["inventory_digest"] == view.digest
    assert result["plan_digest"] == plan["digest"]
    assert result["candidate"] == "onprem-demo"
    assert result["candidate_verdict"] == "needs_work"


def test_map_refuses_inventory_other_than_the_plans():
    plan, _ = plan_and_view()
    doc = inventory_doc()
    doc["items"][0]["rationale"] = "다른 문서"
    _, other = plan_and_view(redigest(doc))
    with pytest.raises(MapError):
        build_map(other, plan, None)


def test_map_refuses_candidate_other_than_the_plans():
    plan, _ = plan_and_view()
    other = parse_inventory(inventory_doc(), candidate="onprem-minimal")
    with pytest.raises(MapError):
        build_map(other, plan, report_for(plan))


@pytest.mark.parametrize("report", [["x"], "x", {"plan_digest": None}])
def test_map_refuses_malformed_report(report):
    plan, view = plan_and_view()
    with pytest.raises(MapError):
        build_map(view, plan, report)


def test_map_refuses_report_of_another_plan():
    plan, view = plan_and_view()
    report = report_for(plan)
    report["plan_digest"] = "sha256:other"
    with pytest.raises(MapError):
        build_map(view, plan, report)


def test_map_is_deterministic():
    plan, view = plan_and_view()
    assert build_map(view, plan, report_for(plan)) == build_map(view, plan, report_for(plan))


def test_markdown_shows_every_entry_and_its_grade():
    plan, view = plan_and_view()
    result = build_map(view, plan, report_for(plan))
    text = render_markdown(result)
    assert "PASS" in text
    for entry in result["entries"]:
        assert entry["name"] in text
    assert "코드 수정" in text and "설정 교체" in text and "어댑터 추가" in text
    assert "등급 없음" in text


def test_markdown_escapes_table_breaking_text():
    plan, view = plan_and_view()
    result = build_map(view, plan, None)
    result["entries"][0]["name"] = "a|b\nc"
    assert "a\\|b c" in render_markdown(result)


def test_markdown_groups_what_remains_first():
    plan, view = plan_and_view()
    text = render_markdown(build_map(view, plan, report_for(plan)))
    assert text.index("남음") < text.index("AWS 경로 막힘")
