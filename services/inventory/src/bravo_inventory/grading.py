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

from .catalog import rationale, target_entry
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


def grade_role(role: str, sdk_calls: list[Finding], env_names: set[str], env_kind: str, source_kind: str | None = None) -> tuple[str, str, bool]:
    """Return (grade, rationale, agent_needed) for one role from its code evidence.

    ``env_kind`` is the target kind. With ``source_kind`` the (source, target, role) hub entry is
    used (same kind → config_swap, pair exceptions); without it the target default (legacy callers).
    """
    entry = target_entry(source_kind, env_kind, role) if source_kind else target_entry(env_kind, role)
    if entry is None:
        return ("unknown", rationale(env_kind, "missing", role=role), True)
    if entry.get("override"):
        return (str(entry["grade"]), str(entry["reason"]), False)
    impl, verify = str(entry["impl"]), str(entry["verify"])
    if entry["grade"] == "none":
        return ("none", rationale(env_kind, "none", verify=verify), False)
    direct = [c for c in sdk_calls if c.kind == "sdk_call"]
    if role == "object_store":
        return _grade_object_store(direct, env_names)
    if role in {"secrets", "kms"}:
        if direct:
            return ("code_change", rationale(env_kind, "direct_sdk", impl=impl), False)
        return ("config_swap", rationale(env_kind, "config_only"), False)
    if role == "iam":
        return ("adapter", rationale(env_kind, "iam", impl=impl), False)
    if role == "db":
        return ("config_swap", rationale(env_kind, "db", impl=impl), False)
    if role in {"runtime", "audit", "registry", "observability", "policy"}:
        return (str(entry["grade"]), str(entry.get("reason") or verify), False)
    if entry["grade"] == "code_change":  # messaging · function · ai on a cloud target
        return ("code_change", rationale(env_kind, "api_change", impl=impl, verify=verify), False)
    return ("unknown", "규칙이 판단하지 못한 역할", True)


def apply_pair(items: list[Item], source_kind: str, target_kind: str, env_names: set[str]) -> list[Item]:
    """Re-grade reconciled items for the (source, target) pair (ADR-0034 role hub).

    ``reconcile`` grades against the target default; this pass applies the same-kind default and
    the pair exceptions. Items whose pair entry is the target default are left untouched, so the
    aws→onprem output is exactly what it was. Rules only: an agent-needed flag is never cleared.
    """
    for it in items:
        if it.role == "unknown" or it.grade_by != "rule":
            continue
        entry = target_entry(source_kind, target_kind, it.role)
        if entry is None or entry["basis"] == "target_default":
            continue
        before = it.rationale
        if it.risk:  # deploy-only: the catalog default for the pair applies
            it.grade = str(entry["grade"])
            it.rationale = f"{before} 조합 {source_kind}→{target_kind}: {entry['reason']}"
        else:
            calls = [Finding(r["id"], r["kind"], it.role, it.service, r["file"], r["line"], r["snippet"], dict(r.get("detail", {}))) for r in it.code_refs]
            grade, text, agent = grade_role(it.role, calls, env_names, target_kind, source_kind)
            it.grade = grade
            it.rationale = text if not it.agent_needed or agent else f"{text} / 규칙 메모: {before}"
            it.agent_needed = it.agent_needed or agent
        it.target = {**it.target, "impl": str(entry["impl"]), "verify": str(entry["verify"]), "source_kind": source_kind, "basis": str(entry["basis"])}
    return items
