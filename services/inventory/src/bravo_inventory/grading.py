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

from .catalog import (
    NON_SQS_TRIGGER_RESOURCES,
    SCOPE_FEATURES,
    service_entries,
    service_for_endpoint_env,
    target_entry,
)
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


def aws_service(f: Finding) -> str | None:
    """The AWS service a finding points at (boto3 name), when the finding names one."""
    if f.kind in {"sdk_call", "unsupported_feature"}:
        return f.service.lower()
    if f.kind == "iac_resource":
        return f.service.removeprefix("aws_").split("_")[0]
    if f.kind in {"env_ref", "config_env"}:
        return service_for_endpoint_env(f.detail.get("name", ""))
    return None


def _where(findings: list[Finding]) -> str:
    return ", ".join(sorted({f"{f.file}:{f.line}" for f in findings}))


def _out_of_scope(findings: list[Finding], features: set[str]) -> str:
    """Label and location of T46 out-of-scope features among the findings, '' when none."""
    hits = [f for f in findings if f.kind == "unsupported_feature" and f.detail.get("feature") in features]
    labels = sorted({SCOPE_FEATURES[f.detail["feature"]][2] for f in hits})
    return f"{' · '.join(labels)} ({_where(hits)})" if hits else ""


def _grade_messaging(code: list[Finding], env_kind: str) -> tuple[str, str, bool]:
    """SQS standard queue → ElasticMQ by `AWS_ENDPOINT_URL_SQS` alone (ADR-0045 1단계). A service
    the target cannot stand in for (SNS) or an out-of-scope feature (FIFO) keeps 'none'."""
    entries = service_entries(env_kind, "messaging", {s for f in code if (s := aws_service(f))})
    missing = [s for s, e in entries.items() if e["grade"] == "none"]
    if missing:
        return ("none", "; ".join(f"{s}: {entries[s]['verify']}" for s in missing), False)
    if scope := _out_of_scope(code, {"sqs_fifo"}):
        return ("none", f"T46 호환 런타임 범위(SQS 표준 큐, ADR-0045) 밖: {scope}. 대체 없음 유지", False)
    # botocore reads AWS_ENDPOINT_URL_SQS unless the code passes its own endpoint_url.
    fixed = [c for c in code if c.kind == "sdk_call" and c.detail.get("endpoint_url", "configurable") != "configurable"]
    if fixed:
        return ("code_change", f"SQS SDK 호출 {len(fixed)}건이 endpoint_url을 코드에 고정했다({_where(fixed)}). AWS_ENDPOINT_URL_SQS가 적용되지 않으므로 설정에서 받도록 코드 수정 필요", False)
    return ("config_swap", "SQS 표준 큐. 앱의 boto3 SQS 호출은 그대로 두고 AWS_ENDPOINT_URL_SQS만 목적지 ElasticMQ로 교체(ADR-0045 1단계)", False)


def _grade_function(code: list[Finding], findings: list[Finding]) -> tuple[str, str, bool]:
    """Lambda handlers run by an SQS event source mapping → T46 compatible runtime. Anything else
    the runtime does not reproduce keeps 'none' (ADR-0045 지원 범위)."""
    direct = [f for f in code if f.kind == "sdk_call" or (f.kind in {"env_ref", "config_env"} and aws_service(f) == "lambda")]
    if direct:
        return ("none", f"앱이 Lambda API를 직접 부른다({_where(direct)}). 호환 런타임은 SQS 이벤트 소스 매핑으로 실행되는 핸들러만 대체한다(ADR-0045). 대체 없음 유지", False)
    triggers = [f for f in code if f.kind == "iac_resource" and f.service in NON_SQS_TRIGGER_RESOURCES]
    scope = _out_of_scope(findings, {k for k, v in SCOPE_FEATURES.items() if v[0] == "function"} | {"sqs_fifo"})
    if triggers or scope:
        labels = sorted({NON_SQS_TRIGGER_RESOURCES[f.service] for f in triggers})
        parts = ([f"{' · '.join(labels)} ({_where(triggers)})"] if triggers else []) + ([scope] if scope else [])
        return ("none", f"T46 호환 런타임 범위(SQS 표준 큐 → 이벤트 소스 매핑, ADR-0045) 밖: {'; '.join(parts)}. 대체 없음 유지", False)
    mapping = any(f.kind == "iac_resource" and f.service == "aws_lambda_event_source_mapping" for f in code)
    sqs = any(f.role == "messaging" and aws_service(f) == "sqs" for f in findings)
    if not (mapping and sqs):
        return ("none", "SQS 이벤트 소스 매핑 근거(aws_lambda_event_source_mapping과 SQS 사용)가 없어 호환 런타임 대상인지 규칙으로 확정할 수 없다. 대체 없음 유지", False)
    return ("adapter", "SQS 이벤트 소스 매핑으로 실행되는 Lambda 핸들러. 핸들러는 고치지 않고 목적지 호환 런타임이 SQS 이벤트 형식으로 부른다(ADR-0045)", False)


def grade_role(
    role: str, sdk_calls: list[Finding], env_names: set[str], env_kind: str, findings: list[Finding] | None = None
) -> tuple[str, str, bool]:
    """Return (grade, rationale, agent_needed) for one role from its code evidence. ``findings``
    is the whole scan, for roles whose grade depends on another role (Lambda needs an SQS source)."""
    entry = target_entry(env_kind, role)
    if entry is None:
        return ("unknown", f"카탈로그에 {env_kind} 목적지의 '{role}' 항목이 없다", True)
    if entry["grade"] == "none":
        return ("none", f"{env_kind}에 대체 구현이 없다: {entry['verify']}", False)
    if role == "messaging":
        return _grade_messaging(sdk_calls, env_kind)
    if role == "function":
        return _grade_function(sdk_calls, findings if findings is not None else sdk_calls)
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
