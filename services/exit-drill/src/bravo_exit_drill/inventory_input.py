"""T16 인벤토리 읽기. T16 출력 형식을 아는 곳은 이 파일뿐이다.

T16 계약(docs/contracts/T16-inventory-v1-proposal.md)은 아직 제안 상태다. 형식이 바뀌면
이 파일과 그 시험만 고친다. 필드 이름뿐 아니라 등급 · 역할 · 환경 종류 값의 뜻도 여기서 풀어,
나머지 모듈은 아래 Dependency · InventoryView만 본다.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .aws_endpoints import is_known_service

SUPPORTED_VERSION = "1.0"
_ITEM_FIELDS = ("id", "role", "service", "found_in", "grade", "grade_by", "agent_needed", "target", "risk")

_REPLACEABLE_GRADES = {"config_swap", "adapter", "code_change"}
_GRADE_LABELS = {
    "config_swap": "설정 교체",
    "adapter": "어댑터 추가",
    "code_change": "코드 수정",
    "none": "대체 없음",
    "unknown": "미지",
}
# T16은 SDK 호출이 없으면 service에 역할 이름을 넣는다. 그때 어떤 AWS API를 재야 하는지 정한다.
# iam은 IAM API 말고도 SDK가 자격을 받는 STS까지 함께 잰다.
# db(CNPG)는 AWS API가 아니라 명세의 출발지 경로로 잰다.
_ROLE_AWS_SERVICES = {
    "object_store": ("s3",),
    "secrets": ("secretsmanager",),
    "kms": ("kms",),
    "iam": ("iam", "sts"),
    "audit": ("logs",),
    "observability": ("cloudwatch",),
    "runtime": ("eks",),
    "registry": ("ecr",),
    "messaging": ("sqs", "sns"),
    "function": ("lambda",),
    "ai": ("bedrock-runtime",),
    "db_managed": ("dynamodb",),
}


class ContractError(ValueError):
    """인벤토리를 믿고 쓸 수 없다. 계약대로 적용을 멈춘다."""


@dataclass(frozen=True)
class Dependency:
    """출발지에서 쓰던 의존성 하나."""

    id: str
    role: str
    services: tuple[str, ...]  # T16이 적은 이름 그대로. 보여 주기용이다.
    aws_services: tuple[str, ...]  # 차단을 재야 할 AWS 서비스. 출발지가 AWS가 아니면 비어 있다.
    grade: str
    grade_by: str
    replacement: str  # replaceable · none · unknown
    awaiting_review: bool  # 규칙이 못 정해 에이전트 확인을 기다린다
    target_impl: str
    risk: str
    found_in: str


@dataclass(frozen=True)
class InventoryView:
    app_id: str
    digest: str
    candidate: str
    candidate_verdict: str
    dependencies: tuple[Dependency, ...]
    residuals: tuple[str, ...]
    missing_capabilities: tuple[str, ...]


def parse_inventory(doc: Any, *, candidate: str) -> InventoryView:
    if not isinstance(doc, dict):
        raise ContractError("inventory must be a JSON object")
    if doc.get("schema_version") != SUPPORTED_VERSION:
        raise ContractError("unsupported inventory schema_version")
    _verify_digest(doc)
    fit = _candidate(doc, candidate)
    try:
        return InventoryView(
            app_id=doc["app"]["id"],
            digest=doc["digest"],
            candidate=candidate,
            candidate_verdict=fit["verdict"],
            dependencies=tuple(_dependency(item, doc["source"]["env_kind"], fit["env_kind"]) for item in doc["items"]),
            residuals=tuple(fit["residual_dependencies"]),
            missing_capabilities=tuple(fit["missing_capabilities"]),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ContractError(f"inventory field missing: {exc}") from exc


def _verify_digest(doc: dict) -> None:
    """T16 report.py의 계산을 그대로 따른다. generated_at만 빠진 문서 전체의 해시다."""
    digest = doc.get("digest")
    if not isinstance(digest, str):
        raise ContractError("inventory digest missing")
    core = {k: v for k, v in doc.items() if k not in {"generated_at", "digest"}}
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if digest != "sha256:" + hashlib.sha256(raw.encode()).hexdigest():
        raise ContractError("inventory digest mismatch")


def _candidate(doc: dict, candidate: str) -> dict:
    for fit in doc.get("candidates") or []:
        if isinstance(fit, dict) and fit.get("env_id") == candidate:
            return fit
    raise ContractError("target candidate not in inventory")


def grade_label(grade: str) -> str:
    return _GRADE_LABELS.get(grade, grade)


def _dependency(item: dict, source_kind: str, candidate_kind: str) -> Dependency:
    for name in _ITEM_FIELDS:
        if name not in item:
            raise ContractError(f"inventory item missing {name}")
    # T16은 코드에서 찾은 SDK 이름을 "s3, kms"처럼 쉼표로 잇는다. 없으면 역할 이름이 들어간다.
    services = tuple(s.strip() for s in item["service"].split(",") if s.strip())
    grade, target = item["grade"], item["target"]
    aws_services: set[str] = set()
    if source_kind == "aws":
        aws_services = {*_ROLE_AWS_SERVICES.get(item["role"], ()), *(s for s in services if is_known_service(s))}
    return Dependency(
        id=item["id"],
        role=item["role"],
        services=services,
        aws_services=tuple(sorted(aws_services)),
        grade=grade,
        grade_by=item["grade_by"],
        replacement=grade if grade == "none" else "replaceable" if grade in _REPLACEABLE_GRADES else "unknown",
        awaiting_review=item["agent_needed"] is True and item["grade_by"] == "rule",
        # T16 항목의 목적지 구현은 첫 후보의 종류 기준이다. 다른 종류의 후보에는 쓸 수 없다.
        target_impl=target.get("impl", "") if target.get("env_kind") == candidate_kind else "",
        risk=item["risk"],
        found_in=item["found_in"],
    )
