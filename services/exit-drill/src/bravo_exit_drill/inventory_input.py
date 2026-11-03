"""T16 인벤토리 읽기. T16 출력 형식을 아는 곳은 이 파일뿐이다.

T16 계약(docs/contracts/T16-inventory-v1-proposal.md)은 아직 제안 상태다. 형식이 바뀌면
이 파일과 그 시험만 고친다. 나머지 모듈은 아래 Dependency · InventoryView만 본다.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SUPPORTED_VERSION = "1.0"
_ITEM_FIELDS = ("id", "role", "service", "found_in", "grade", "grade_by", "agent_needed", "target", "risk")


class ContractError(ValueError):
    """인벤토리를 믿고 쓸 수 없다. 계약대로 적용을 멈춘다."""


@dataclass(frozen=True)
class Dependency:
    """출발지에서 쓰던 의존성 하나."""

    id: str
    role: str
    services: tuple[str, ...]
    grade: str
    grade_by: str
    agent_needed: bool
    target_impl: str
    risk: str
    found_in: str


@dataclass(frozen=True)
class InventoryView:
    app_id: str
    source_kind: str
    digest: str
    candidate: str
    candidate_verdict: str
    dependencies: tuple[Dependency, ...]
    residuals: tuple[str, ...]


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
            source_kind=doc["source"]["env_kind"],
            digest=doc["digest"],
            candidate=candidate,
            candidate_verdict=fit["verdict"],
            dependencies=tuple(_dependency(item) for item in doc["items"]),
            residuals=tuple(fit["residual_dependencies"]),
        )
    except (KeyError, TypeError) as exc:
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


def _dependency(item: dict) -> Dependency:
    for name in _ITEM_FIELDS:
        if name not in item:
            raise ContractError(f"inventory item missing {name}")
    # T16은 코드에서 찾은 SDK 이름을 "s3, kms"처럼 쉼표로 잇는다. 없으면 역할 이름이 들어간다.
    services = tuple(s.strip() for s in item["service"].split(",") if s.strip())
    return Dependency(
        id=item["id"],
        role=item["role"],
        services=services,
        grade=item["grade"],
        grade_by=item["grade_by"],
        agent_needed=item["agent_needed"] is True,
        target_impl=item["target"].get("impl", ""),
        risk=item["risk"],
        found_in=item["found_in"],
    )
