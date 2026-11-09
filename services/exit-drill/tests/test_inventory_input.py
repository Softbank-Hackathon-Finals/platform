"""T16 형식 의존은 inventory_input 한 곳에만 있다. 고정 입력은 T16 코드가 실제로 낸 출력이다."""

import ast
from pathlib import Path

import pytest
from helpers import inventory_doc, redigest

from bravo_exit_drill import inventory_input
from bravo_exit_drill.inventory_input import ContractError, parse_inventory


def test_reads_real_t16_output():
    view = parse_inventory(inventory_doc(), candidate="onprem-demo")
    assert view.app_id == "demo-chat"
    assert view.digest.startswith("sha256:")
    assert [d.role for d in view.dependencies] == ["db", "iam", "kms", "object_store", "secrets"]
    s3 = next(d for d in view.dependencies if d.role == "object_store")
    assert (s3.id, s3.services, s3.grade, s3.grade_by, s3.target_impl, s3.replacement) == (
        "item-004", ("s3",), "config_swap", "rule", "seaweedfs", "replaceable",
    )
    assert s3.aws_services == ("s3",)
    kms = next(d for d in view.dependencies if d.role == "kms")
    assert kms.awaiting_review is True
    assert kms.risk == "migration_omission"
    db = next(d for d in view.dependencies if d.role == "db")
    assert db.aws_services == ()
    iam = next(d for d in view.dependencies if d.role == "iam")
    assert iam.aws_services == ("iam", "sts")
    assert view.residuals == ("cloudflare (edge · tunnel)", "ghcr.io (image pull)")
    assert view.candidate_verdict == "needs_work"


def test_candidate_selects_its_own_residuals():
    view = parse_inventory(inventory_doc(), candidate="onprem-minimal")
    assert view.residuals == ("cloudflare (edge · tunnel)",)
    assert view.candidate_verdict == "blocked"


def test_agent_graded_item_is_not_awaiting_review():
    doc = inventory_doc()
    doc["items"][2]["grade_by"] = "agent"
    view = parse_inventory(redigest(doc), candidate="onprem-demo")
    assert next(d for d in view.dependencies if d.role == "kms").awaiting_review is False


@pytest.mark.parametrize(("grade", "expected"), [
    ("config_swap", "replaceable"), ("adapter", "replaceable"), ("code_change", "replaceable"),
    ("none", "none"), ("unknown", "unknown"), ("rewrite", "unknown"),
])
def test_grade_becomes_replacement_status(grade, expected):
    doc = inventory_doc()
    doc["items"][3]["grade"] = grade
    view = parse_inventory(redigest(doc), candidate="onprem-demo")
    assert next(d for d in view.dependencies if d.id == "item-004").replacement == expected


def test_source_other_than_aws_has_no_aws_services():
    doc = inventory_doc()
    doc["source"]["env_kind"] = "gcp"
    view = parse_inventory(redigest(doc), candidate="onprem-demo")
    assert all(d.aws_services == () for d in view.dependencies)


def test_target_implementation_of_another_kind_is_not_used():
    """T16 항목의 목적지 구현은 첫 후보 기준이다. 종류가 다른 후보에는 쓸 수 없다."""
    doc = inventory_doc()
    doc["candidates"][1]["env_kind"] = "gcp"
    view = parse_inventory(redigest(doc), candidate="onprem-minimal")
    assert all(d.target_impl == "" for d in view.dependencies)


def test_comma_joined_service_becomes_separate_services():
    doc = inventory_doc()
    doc["items"][3]["service"] = "s3, kms"
    view = parse_inventory(redigest(doc), candidate="onprem-demo")
    assert next(d for d in view.dependencies if d.role == "object_store").services == ("s3", "kms")


def test_unknown_schema_version_stops():
    """계약: 인식하지 못하는 버전이면 소비자는 적용을 멈춘다."""
    doc = inventory_doc()
    doc["schema_version"] = "2.0"
    with pytest.raises(ContractError, match="schema_version"):
        parse_inventory(redigest(doc), candidate="onprem-demo")


def test_tampered_document_stops():
    doc = inventory_doc()
    doc["items"][0]["grade"] = "none"
    with pytest.raises(ContractError, match="digest"):
        parse_inventory(doc, candidate="onprem-demo")


def test_missing_digest_stops():
    doc = inventory_doc()
    del doc["digest"]
    with pytest.raises(ContractError, match="digest"):
        parse_inventory(doc, candidate="onprem-demo")


def test_generated_at_alone_does_not_change_identity():
    doc = inventory_doc()
    doc["generated_at"] = "2030-01-01T00:00:00Z"
    assert parse_inventory(doc, candidate="onprem-demo").digest == inventory_doc()["digest"]


def test_unregistered_candidate_stops():
    with pytest.raises(ContractError, match="candidate"):
        parse_inventory(inventory_doc(), candidate="nowhere")


@pytest.mark.parametrize("field", ["id", "role", "service", "grade", "grade_by", "agent_needed", "target", "risk", "found_in"])
def test_item_missing_a_field_stops(field):
    doc = inventory_doc()
    del doc["items"][0][field]
    with pytest.raises(ContractError, match=field):
        parse_inventory(redigest(doc), candidate="onprem-demo")


@pytest.mark.parametrize("mutate", [
    lambda d: d["items"][0].update(service=None),
    lambda d: d["items"][0].update(target=None),
    lambda d: d.update(items=[1]),
    lambda d: d.update(app=None),
])
def test_wrong_field_types_stop(mutate):
    doc = inventory_doc()
    mutate(doc)
    with pytest.raises(ContractError):
        parse_inventory(redigest(doc), candidate="onprem-demo")


def test_trace_notes_are_read_as_written():
    """T16이 배포를 따라가다 못 읽은 입력을 적는 곳이다. 지도가 그대로 보여 준다."""
    doc = inventory_doc()
    doc["deploy"]["notes"] = ["Deployment/a → ServiceAccount/b: IRSA 역할 주석 없음"]
    view = parse_inventory(redigest(doc), candidate="onprem-demo")
    assert view.trace_notes == ("Deployment/a → ServiceAccount/b: IRSA 역할 주석 없음",)


@pytest.mark.parametrize("mutate", [
    lambda d: d["deploy"].pop("notes"),
    lambda d: d.pop("deploy"),
    lambda d: d["deploy"].update(notes="한 줄"),
    lambda d: d["deploy"].update(notes=[{"note": "x"}]),
])
def test_missing_or_malformed_trace_notes_stop(mutate):
    """메모가 없다고 읽으면 못 잰 입력을 다 쟀다고 말하게 된다."""
    doc = inventory_doc()
    mutate(doc)
    with pytest.raises(ContractError):
        parse_inventory(redigest(doc), candidate="onprem-demo")


def test_not_a_document_stops():
    with pytest.raises(ContractError):
        parse_inventory([], candidate="onprem-demo")


def test_t16_field_names_live_in_one_module():
    """다른 모듈이 T16 값을 직접 다루면 계약이 바뀔 때 고칠 곳이 흩어진다.

    grade_by · found_in은 지도가 내보내는 필드 이름이기도 해서 여기서 보지 않는다.
    """
    source_dir = Path(inventory_input.__file__).parent
    t16_only = {
        "agent_needed", "residual_dependencies", "candidates",
        "config_swap", "code_change", "object_store", "env_kind",
    }
    for path in source_dir.glob("*.py"):
        if path.name == "inventory_input.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert not (constants & t16_only), f"{path.name}가 T16 값을 직접 다룬다: {constants & t16_only}"
