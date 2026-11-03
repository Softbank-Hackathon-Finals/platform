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
    assert view.source_kind == "aws"
    assert view.digest.startswith("sha256:")
    assert [d.role for d in view.dependencies] == ["db", "iam", "kms", "object_store", "secrets"]
    s3 = next(d for d in view.dependencies if d.role == "object_store")
    assert (s3.id, s3.services, s3.grade, s3.grade_by, s3.target_impl) == (
        "item-004", ("s3",), "config_swap", "rule", "seaweedfs",
    )
    kms = next(d for d in view.dependencies if d.role == "kms")
    assert kms.agent_needed is True
    assert kms.risk == "migration_omission"
    assert view.residuals == ("cloudflare (edge · tunnel)", "ghcr.io (image pull)")
    assert view.candidate_verdict == "needs_work"


def test_candidate_selects_its_own_residuals():
    view = parse_inventory(inventory_doc(), candidate="onprem-minimal")
    assert view.residuals == ("cloudflare (edge · tunnel)",)
    assert view.candidate_verdict == "blocked"


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


def test_not_a_document_stops():
    with pytest.raises(ContractError):
        parse_inventory([], candidate="onprem-demo")


def test_t16_field_names_live_in_one_module():
    """다른 모듈이 T16 필드를 직접 읽으면 계약이 바뀔 때 고칠 곳이 흩어진다."""
    source_dir = Path(inventory_input.__file__).parent
    t16_only = {"grade_by", "agent_needed", "residual_dependencies", "found_in", "candidates"}
    for path in source_dir.glob("*.py"):
        if path.name == "inventory_input.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        subscripts = {
            node.value
            for sub in ast.walk(tree)
            if isinstance(sub, ast.Subscript)
            for node in ast.walk(sub.slice)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not (subscripts & t16_only), f"{path.name}가 T16 필드를 직접 읽는다"
