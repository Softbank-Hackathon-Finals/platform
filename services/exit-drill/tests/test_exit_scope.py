"""출구 시험 차단 대상 등록부(ADR-0034 `source.exit_scope`).

9개 조합마다 막을 출발지 주소가 있어야 하고, 같은 종류 간 이전에서는 목적지 범위를 막지 않는다.
호스트로 가를 수 없는 의존성은 숨기지 않고 '범위로 구분 불가'로 드러나 판정이 통과가 되지 않는다.
"""

import itertools
import json
from pathlib import Path

import pytest
from helpers import inventory_doc, redigest, snapshot_doc, spec_doc
from test_verdict import good_evidence

from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.exit_scope import (
    CLUSTER_RELATIVE_NAME,
    ENDPOINT_SUFFIXES,
    GLOBAL_ENDPOINT,
    RESOURCE_IN_TARGET_SCOPE,
    SCOPE_KEYS,
    SHARED_REGIONAL_ENDPOINT,
    split_endpoint,
)
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.policy import PlanRefused, build_plan, exit_scope_for, plan_is_intact
from bravo_exit_drill.residual_map import build_map
from bravo_exit_drill.spec import SpecError, parse_spec
from bravo_exit_drill.verdict import judge

CONTRACT = Path(__file__).parents[3] / "docs" / "contracts" / "environment-kinds.json"
KINDS = ("aws", "gcp", "onprem")
CANDIDATE = "onprem-demo"
# 값은 모두 지어낸 자리표시다. 실제 계정 · 프로젝트 식별자를 쓰지 않는다.
SOURCE_SCOPE = {
    "aws": {"account_id": "src-account", "region": "ap-northeast-2"},
    "gcp": {"project_id": "src-project", "region": "asia-northeast3"},
    "onprem": {"cluster_uid": "src-cluster-uid"},
}
TARGET_SCOPE = {
    "aws": {"account_id": "dst-account", "region": "ap-northeast-1"},
    "gcp": {"project_id": "dst-project", "region": "asia-northeast1"},
    "onprem": {"cluster_uid": "dst-cluster-uid"},
}
SOURCE_TAILNET = "src.example.ts.net"
TARGET_TAILNET = "dst.example.ts.net"


def _resource(kind: str, name: str, role: str, **attributes: str) -> dict:
    return {"kind": kind, "name": name, "role": role, "via": "Namespace/chat", "actions": [], "attributes": attributes}


def inventory_for(source: str, target: str) -> dict:
    """demo-chat 인벤토리를 출발지 · 목적지 종류만 바꿔 쓴다. 종류별 출발지 자원을 더한다."""
    doc = inventory_doc()
    doc["source"]["env_kind"] = source
    next(c for c in doc["candidates"] if c["env_id"] == CANDIDATE)["env_kind"] = target
    resources = doc["deploy"]["resources"]
    if source == "gcp":
        resources.append(_resource("gcs_bucket", "src-chat-attachments", "object_store", project_id="src-project"))
        resources.append(_resource("gcs_bucket", "dst-shared-assets", "object_store", project_id="dst-project"))
    if source == "onprem":
        resources.extend([
            _resource("seaweedfs_bucket", "attachments", "object_store",
                      endpoint=f"http://seaweedfs.{SOURCE_TAILNET}:8333", cluster_uid="src-cluster-uid"),
            _resource("openbao_mount", "kv", "secrets", endpoint=f"https://openbao.{SOURCE_TAILNET}"),
            _resource("cnpg_cluster", "chat-db", "db", endpoint=f"postgres://source-db.{SOURCE_TAILNET}"),
            # 출발지 클러스터 안에서만 뜻이 있는 이름. 목적지 클러스터에서는 목적지 자신을 가리킨다.
            _resource("openbao_transit", "transit", "kms", endpoint="http://openbao.openbao.svc:8200"),
            # 인벤토리가 목적지 클러스터의 자원을 가리킨 경우.
            _resource("openbao_mount", "shared", "iam",
                      endpoint=f"https://shared-bao.{TARGET_TAILNET}:8200", cluster_uid="dst-cluster-uid"),
        ])
    return redigest(doc)


def spec_for(source: str, target: str, *, target_scope: dict | None = None) -> dict:
    doc = spec_doc()
    del doc["aws_region"]
    doc["source_scope"] = dict(SOURCE_SCOPE[source])
    doc["target_scope"] = dict(TARGET_SCOPE[target] if target_scope is None else target_scope)
    return doc


def plan_for(source: str, target: str, **kwargs) -> dict:
    return build_plan(
        parse_spec(spec_for(source, target, **kwargs)),
        parse_inventory(inventory_for(source, target), candidate=CANDIDATE),
        parse_snapshot(snapshot_doc(), namespace="target-dev"),
    )


def scope_for(source: str, target: str, **kwargs):
    return exit_scope_for(
        parse_spec(spec_for(source, target, **kwargs)),
        parse_inventory(inventory_for(source, target), candidate=CANDIDATE),
    )


def derived_blocks(plan: dict) -> dict[str, dict]:
    """명세가 적은 경로(pooler-to-source)를 빼고 등록부가 만든 차단 경로만."""
    return {d["route_id"]: d for d in plan["decisions"] if d["action"] == "BLOCK" and d["route_id"] != "pooler-to-source"}


def blocked_hosts(plan: dict) -> set[str]:
    return {d["probe"]["host"] for d in derived_blocks(plan).values()}


def indistinct(plan: dict) -> dict[str, dict]:
    return {i["id"]: i for i in plan.get("scope_indistinct", [])}


@pytest.mark.parametrize(("source", "target"), list(itertools.product(KINDS, KINDS)))
def test_every_combination_has_source_addresses_to_block(source, target):
    plan = plan_for(source, target)
    assert derived_blocks(plan), f"{source}→{target}: 차단 대상이 없다"
    assert plan_is_intact(plan)
    for decision in derived_blocks(plan).values():
        assert decision["owners"] == ["app"]
        assert decision["roles"]


@pytest.mark.parametrize(("source", "target"), list(itertools.product(KINDS, KINDS)))
def test_blocked_addresses_belong_to_the_source_kind(source, target):
    # 온프렘은 공개 주소 접미사가 없다. 인벤토리가 알려 준 tailnet 주소만 막는다.
    suffixes = ENDPOINT_SUFFIXES[source] or (".ts.net",)
    assert all(host.endswith(suffixes) for host in blocked_hosts(plan_for(source, target)))


def test_gcp_source_blocks_google_apis_and_its_buckets():
    hosts = blocked_hosts(plan_for("gcp", "onprem"))
    assert {
        "storage.googleapis.com", "secretmanager.googleapis.com", "cloudkms.googleapis.com",
        "iamcredentials.googleapis.com", "sts.googleapis.com", "src-chat-attachments.storage.googleapis.com",
    } <= hosts
    assert not any(h.endswith("amazonaws.com") for h in hosts)


def test_aws_to_aws_does_not_block_the_target_scope():
    """다른 리전 · 다른 계정의 AWS로 옮긴다. 출발지 리전 주소와 출발지 버킷만 막고 공통 · 목적지 주소는 남긴다."""
    plan = plan_for("aws", "aws")
    hosts = blocked_hosts(plan)
    assert "s3.ap-northeast-2.amazonaws.com" in hosts
    assert "demo-chat-attachments.s3.ap-northeast-2.amazonaws.com" in hosts
    assert not any("ap-northeast-1" in h for h in hosts)
    assert "iam.amazonaws.com" not in hosts
    assert indistinct(plan)["aws-iam"]["reason"] == GLOBAL_ENDPOINT


def test_aws_to_aws_in_the_same_region_blocks_only_source_buckets():
    """같은 리전이면 서비스 공통 주소는 목적지도 쓴다. 통째로 막지 않고 범위로 구분 불가로 드러낸다."""
    plan = plan_for("aws", "aws", target_scope={"account_id": "dst-account", "region": "ap-northeast-2"})
    assert blocked_hosts(plan) == {
        "demo-chat-attachments.s3.ap-northeast-2.amazonaws.com",
        "chat-audit-archive.s3.ap-northeast-2.amazonaws.com",
    }
    assert {i["id"]: i["reason"] for i in indistinct(plan).values()} == {
        "aws-iam": GLOBAL_ENDPOINT,
        "aws-kms": SHARED_REGIONAL_ENDPOINT,
        "aws-s3": SHARED_REGIONAL_ENDPOINT,
        "aws-secretsmanager": SHARED_REGIONAL_ENDPOINT,
        "aws-sts": SHARED_REGIONAL_ENDPOINT,
    }
    # 드러낸 역할은 '잴 경로 없음'으로 겹쳐 적지 않는다.
    assert plan["unmapped_dependencies"] == []


def test_aws_to_onprem_is_unchanged():
    """기존 쌍은 같은 차단 경로 · 같은 계획 모양이다. 버킷 주소를 더하지 않고 구분 불가 칸도 없다."""
    plan = plan_for("aws", "onprem")
    assert set(derived_blocks(plan)) == {"aws-iam", "aws-kms", "aws-s3", "aws-secretsmanager", "aws-sts"}
    assert "scope_indistinct" not in plan


def test_gcp_to_gcp_does_not_block_the_target_project():
    plan = plan_for("gcp", "gcp")
    hosts = blocked_hosts(plan)
    assert "src-chat-attachments.storage.googleapis.com" in hosts
    assert "storage.asia-northeast3.rep.googleapis.com" in hosts
    # 공통 주소와 목적지 프로젝트의 버킷, 목적지 리전 주소는 막지 않는다.
    assert not {"storage.googleapis.com", "secretmanager.googleapis.com", "dst-shared-assets.storage.googleapis.com"} & hosts
    assert not any("asia-northeast1" in h for h in hosts)
    found = indistinct(plan)
    assert found["gcp-storage"]["reason"] == GLOBAL_ENDPOINT
    assert found["gcp-gcs-bucket-dst-shared-assets"]["reason"] == RESOURCE_IN_TARGET_SCOPE


def test_onprem_to_onprem_blocks_only_source_cluster_addresses():
    plan = plan_for("onprem", "onprem")
    assert blocked_hosts(plan) == {
        f"seaweedfs.{SOURCE_TAILNET}", f"openbao.{SOURCE_TAILNET}", f"source-db.{SOURCE_TAILNET}",
    }
    ports = {d["probe"]["host"]: d["probe"]["port"] for d in derived_blocks(plan).values()}
    assert ports == {f"seaweedfs.{SOURCE_TAILNET}": 8333, f"openbao.{SOURCE_TAILNET}": 443, f"source-db.{SOURCE_TAILNET}": 5432}
    found = indistinct(plan)
    assert found["onprem-kms-transit"]["reason"] == CLUSTER_RELATIVE_NAME
    assert found["onprem-iam-shared"]["reason"] == RESOURCE_IN_TARGET_SCOPE


def test_onprem_to_another_kind_still_hides_nothing():
    """목적지가 다른 종류여도 클러스터 안 이름은 출발지를 가리킨다고 말할 수 없다."""
    plan = plan_for("onprem", "aws")
    assert f"shared-bao.{TARGET_TAILNET}" in blocked_hosts(plan)  # 범위 비교는 같은 종류일 때만 한다
    assert indistinct(plan)["onprem-kms-transit"]["reason"] == CLUSTER_RELATIVE_NAME


def test_same_cluster_is_not_a_migration():
    with pytest.raises(PlanRefused) as caught:
        plan_for("onprem", "onprem", target_scope={"cluster_uid": "src-cluster-uid"})
    assert "SAME_CLUSTER" in {v.code for v in caught.value.violations}


@pytest.mark.parametrize("kind", KINDS)
def test_same_kind_needs_both_scopes(kind):
    """목적지 범위를 모르면 무엇이 출발지인지 모른다. 반쯤 고른 목록으로 계획을 만들지 않는다."""
    with pytest.raises(PlanRefused) as caught:
        plan_for(kind, kind, target_scope={})
    assert {(v.code, v.subject) for v in caught.value.violations} >= {("SCOPE_KEY_MISSING", f"target.{key}") for key in SCOPE_KEYS[kind]}


def test_scope_key_of_another_kind_is_refused():
    spec = spec_for("gcp", "onprem")
    spec["source_scope"]["account_id"] = "src-account"
    with pytest.raises(PlanRefused) as caught:
        build_plan(parse_spec(spec), parse_inventory(inventory_for("gcp", "onprem"), candidate=CANDIDATE),
                   parse_snapshot(snapshot_doc(), namespace="target-dev"))
    assert ("SCOPE_KEY_UNKNOWN", "source.account_id") in {(v.code, v.subject) for v in caught.value.violations}


def test_unknown_source_kind_is_unsupported():
    doc = inventory_for("aws", "onprem")
    doc["source"]["env_kind"] = "azure"
    with pytest.raises(PlanRefused) as caught:
        build_plan(parse_spec(spec_doc()), parse_inventory(redigest(doc), candidate=CANDIDATE),
                   parse_snapshot(snapshot_doc(), namespace="target-dev"))
    assert "UNSUPPORTED_SOURCE_KIND" in {v.code for v in caught.value.violations}


def test_indistinct_dependency_keeps_the_drill_from_passing():
    """나머지 증거가 모두 맞아도 범위로 구분 불가 항목이 있으면 UNKNOWN이다."""
    plan = plan_for("aws", "aws", target_scope={"account_id": "dst-account", "region": "ap-northeast-2"})
    before, after = good_evidence(plan)
    report = judge(plan, before, after)
    not_pass = {c["id"]: c["code"] for c in report["checks"] if c["verdict"] != "PASS"}
    assert report["verdict"] == "UNKNOWN"
    assert set(not_pass.values()) == {"SCOPE_INDISTINCT"}
    assert "scope:aws-s3" in not_pass


def test_indistinct_dependency_is_on_the_map():
    plan = plan_for("aws", "aws", target_scope={"account_id": "dst-account", "region": "ap-northeast-2"})
    view = parse_inventory(inventory_for("aws", "aws"), candidate=CANDIDATE)
    before, after = good_evidence(plan)
    entries = {e["id"]: e for e in build_map(view, plan, judge(plan, before, after))["entries"]}
    assert "scope:aws-secretsmanager" in entries["item-005"]["evidence"]
    assert entries["item-005"]["status"] != "blocked_app_ok"


def test_editing_indistinct_list_breaks_the_plan():
    plan = plan_for("gcp", "gcp")
    assert plan_is_intact(plan)
    assert not plan_is_intact({**plan, "scope_indistinct": []})
    assert not plan_is_intact({k: v for k, v in plan.items() if k != "scope_indistinct"})


def test_legacy_aws_region_still_reads():
    spec = parse_spec(spec_doc())
    assert spec.source_scope == {"region": "ap-northeast-2"}
    assert spec.aws_region == "ap-northeast-2"
    assert spec.target_scope == {}


def test_spec_takes_exactly_one_source_scope():
    spec = spec_for("aws", "aws")
    spec["aws_region"] = "ap-northeast-2"
    with pytest.raises(SpecError):
        parse_spec(spec)
    del spec["aws_region"], spec["source_scope"]
    with pytest.raises(SpecError):
        parse_spec(spec)


def test_spec_rejects_scope_keys_outside_the_contract():
    spec = spec_for("aws", "aws")
    spec["target_scope"]["tenant"] = "x"
    with pytest.raises(SpecError):
        parse_spec(spec)


def test_scope_for_returns_registry_result():
    scope = scope_for("onprem", "gcp")
    assert (scope.source_kind, scope.target_kind) == ("onprem", "gcp")
    assert scope.violations == ()


@pytest.mark.parametrize(("endpoint", "expected"), [
    ("http://seaweedfs.example.ts.net:8333/bucket", ("seaweedfs.example.ts.net", 8333)),
    ("https://openbao.example.ts.net", ("openbao.example.ts.net", 443)),
    ("postgres://user@db.example.ts.net/chat", ("db.example.ts.net", 5432)),
    ("100.64.0.7:5432", ("100.64.0.7", 5432)),
    ("db.example.ts.net:not-a-port", ("", None)),
    ("[fd7a::1]:5432", ("", None)),
])
def test_split_endpoint(endpoint, expected):
    assert split_endpoint(endpoint) == expected


def test_registry_matches_the_kind_contract():
    """종류 · 범위 키 · 주소 접미사의 원본은 environment-kinds.json이다. 갈라지면 시험이 실패한다."""
    if not CONTRACT.exists():  # 서비스 폴더만 복사해 도는 변이 시험(mutation_check.py)용. 저장소에서는 늘 있다.
        pytest.skip("kind contract is outside this copy")
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))["kinds"]
    assert set(contract) == set(SCOPE_KEYS) == set(ENDPOINT_SUFFIXES)
    for kind, item in contract.items():
        assert tuple(item["scope_keys"]) == SCOPE_KEYS[kind]
        assert tuple(item["endpoint_suffixes"]) == ENDPOINT_SUFFIXES[kind]
