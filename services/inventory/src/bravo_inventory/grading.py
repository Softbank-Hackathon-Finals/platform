"""Three-grade classification (design FR-2, 7.3). Rules first; only what the rules cannot
decide is marked ``agent_needed`` for T11. The agent never overrides a rule-set grade.

  config_swap  endpoint/host comes from configuration and the target offers a drop-in
  adapter      the dependency is reachable through a translation layer (IAM → RBAC/policy)
  code_change  the SDK is called directly for a role whose target needs a different API
  none         the catalog has no replacement for this role at the target
  unknown      rules could not decide → agent_needed
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import target_entry
from .static_scan import Finding


@dataclass
class Item:
    id: str
    role: str
    service: str
    found_in: str  # code | deploy | both
    grade: str
    grade_by: str  # rule | agent
    rationale: str
    agent_needed: bool = False
    code_refs: list[dict] = field(default_factory=list)
    deploy_refs: list[dict] = field(default_factory=list)
    target: dict[str, str] = field(default_factory=dict)
    risk: str = ""  # migration_omission for deploy-only items

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "service": self.service,
            "found_in": self.found_in,
            "grade": self.grade,
            "grade_by": self.grade_by,
            "rationale": self.rationale,
            "agent_needed": self.agent_needed,
            "code_refs": self.code_refs,
            "deploy_refs": self.deploy_refs,
            "target": self.target,
            "risk": self.risk,
        }


def _env_names(findings: list[Finding]) -> set[str]:
    return {f.detail.get("name", "") for f in findings if f.kind in {"env_ref", "config_env"}}


def _grade_object_store(direct: list[Finding], env_names: set[str]) -> tuple[str, str, bool]:
    """Every S3 client factory call is graded on its own; the role takes the most demanding
    grade. One ``boto3.client("s3")`` without ``endpoint_url`` is enough to require a code
    change, however many other calls read the endpoint from configuration."""
    if not direct:
        if any(n.endswith("_ENDPOINT_URL") for n in env_names):
            return ("config_swap", "SDK 직접 호출 없이 설정(endpoint 환경변수)으로만 참조", False)
        return ("config_swap", "SDK 직접 호출 없이 설정으로만 참조", False)
    configurable = [c for c in direct if c.detail.get("endpoint_url") == "configurable"]
    fixed = [c for c in direct if c not in configurable]
    if not fixed:
        return ("config_swap", f"S3 SDK 호출 {len(direct)}건 모두 endpoint_url이 설정에서 오므로 S3 호환 저장소로 주소만 교체", False)
    where = ", ".join(sorted({f"{c.file}:{c.line}" for c in fixed}))
    if not configurable:
        return ("code_change", f"S3 SDK 호출 {len(fixed)}건에 endpoint 설정이 없다({where}). endpoint_url을 설정에서 받도록 코드 수정 필요", False)
    return (
        "code_change",
        f"S3 SDK 호출 {len(direct)}건 중 {len(fixed)}건은 endpoint 설정이 없다({where}). 설정 가능한 호출과 같은 설정을 공유하는지 규칙으로 확정할 수 없어 코드 수정으로 보고 에이전트 확인 필요",
        True,
    )


def grade_role(role: str, sdk_calls: list[Finding], env_names: set[str], env_kind: str) -> tuple[str, str, bool]:
    """Return (grade, rationale, agent_needed) for one role from its code evidence."""
    entry = target_entry(env_kind, role)
    if entry is None:
        return ("unknown", f"카탈로그에 {env_kind} 목적지의 '{role}' 항목이 없다", True)
    if entry["grade"] == "none":
        return ("none", f"{env_kind}에 대체 구현이 없다: {entry['verify']}", False)
    direct = [c for c in sdk_calls if c.kind == "sdk_call"]
    if role == "object_store":
        return _grade_object_store(direct, env_names)
    if role in {"secrets", "kms"}:
        if direct:
            return ("code_change", f"{entry['impl']}로 바꾸려면 SDK 직접 호출을 주입 방식으로 고쳐야 한다", False)
        return ("config_swap", "설정 참조만 있어 주입 경로 교체로 충분", False)
    if role == "iam":
        return ("adapter", "IAM 역할·정책은 목적지 RBAC·저장소 정책으로 번역한다", False)
    if role == "db":
        return ("config_swap", "같은 엔진(CNPG). 접속 주소만 교체, 데이터는 복제로 이전", False)
    if role in {"runtime", "audit", "registry", "observability", "policy"}:
        return (entry["grade"], entry["verify"], False)
    return ("unknown", "규칙이 판단하지 못한 역할", True)
