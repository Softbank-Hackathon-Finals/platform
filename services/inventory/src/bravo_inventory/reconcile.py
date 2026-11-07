"""Join code findings with the deployment graph into inventory items (design 8.11 대조)."""

from __future__ import annotations

from collections import defaultdict

from .catalog import target_entry
from .deploy_trace import DeployGraph
from .grading import Item, _env_names, aws_service, grade_role
from .static_scan import Finding

ROLE_KINDS = {"object_store": "s3_bucket", "secrets": "secret", "kms": "kms_key", "iam": "iam_role", "db": "cnpg_cluster", "registry": "image"}
SPLIT_KINDS = frozenset({"s3_bucket", "secret", "kms_key"})  # named by a workload setting, or deploy-only


def _code_refs(findings: list[Finding]) -> list[dict]:
    return [{"id": f.id, "kind": f.kind, "file": f.file, "line": f.line, "snippet": f.snippet, **({"detail": f.detail} if f.detail else {})} for f in findings]


def _deploy_refs(resources) -> list[dict]:
    return [{"kind": r.kind, "name": r.name, "via": r.via, "actions": r.actions, "attributes": r.attributes} for r in resources]


def reconcile(findings: list[Finding], graph: DeployGraph | None, env_kind: str) -> list[Item]:
    by_role: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.kind in {"sdk_call", "env_ref", "config_env", "iac_resource", "unsupported_feature"} and f.role != "unknown":
            by_role[f.role].append(f)
    env_names = _env_names(findings)
    deploy_by_role: dict[str, list] = defaultdict(list)
    if graph:
        for r in graph.resources:
            deploy_by_role[r.role].append(r)

    items: list[Item] = []
    n = 0
    # Images are shown under deploy.workloads; the registry is a residual dependency of the
    # target profile, not a dependency *of the app* to be graded.
    roles = sorted(set(by_role) | {r for r in deploy_by_role if r not in {"unknown", "registry"}})
    for role in roles:
        n += 1
        code = by_role.get(role, [])
        deploy = deploy_by_role.get(role, [])
        # The code reaches a role's resource through a workload setting. A granted bucket/secret/key
        # that no setting names is not covered by the code evidence: it gets its own deploy-only
        # item instead of hiding inside the role's `both` item.
        unreferenced = [r for r in deploy if code and r.kind in SPLIT_KINDS and "referenced_by_env" not in r.attributes]
        deploy = [r for r in deploy if r not in unreferenced]
        found_in = "both" if code and deploy else "code" if code else "deploy"
        grade, rationale, agent_needed = grade_role(role, code, env_names, env_kind, findings)
        entry = target_entry(env_kind, role, {s for f in code if (s := aws_service(f))}) or {}
        if code and grade == "none" and entry.get("impl"):
            # The role has a target, but this app uses something it does not cover (T46 범위 밖 기능).
            entry = {"impl": "", "verify": f"대체 없음: {rationale}"}
        item = Item(
            id=f"item-{n:03d}", role=role, service=", ".join(sorted({f.service for f in code if f.kind == "sdk_call"})) or role,
            found_in=found_in, grade=grade, grade_by="rule", rationale=rationale, agent_needed=agent_needed,
            code_refs=_code_refs(code), deploy_refs=_deploy_refs(deploy),
            target={"env_kind": env_kind, "impl": entry.get("impl", ""), "verify": entry.get("verify", "")},
        )
        if found_in == "deploy":
            # The code never calls this role, but a policy grants it: implicit use (SSE-KMS, rotation
            # lambda, ops-added permission). The catalog default grade applies and the agent must
            # explain how the app actually depends on it before the item can be trusted.
            item.risk = "migration_omission"
            item.grade = entry.get("grade", "unknown") if entry else "unknown"
            item.agent_needed = True
            item.rationale = "배포에만 있음(코드 밖에서 만든 설정). 이전에서 빠뜨리기 쉬운 항목. 코드에 호출이 없으므로 암묵적 사용 여부를 에이전트가 확인한다. 카탈로그 기본 등급 적용"
        if role == "iam" and any(r.kind == "wildcard" for r in deploy):
            item.rationale += " 정책에 와일드카드 자원이 있어 어떤 자원을 쓰는지 확정할 수 없다 → 에이전트 분류 필요"
            item.agent_needed = True
        items.append(item)
        if unreferenced:
            n += 1
            items.append(Item(
                id=f"item-{n:03d}", role=role, service=role, found_in="deploy",
                grade=entry.get("grade", "unknown") if entry else "unknown", grade_by="rule",
                rationale="배포에만 있음: 권한은 있으나 워크로드 설정이 이 자원을 가리키지 않는다(SecretStore 주입 · 운영 추가 권한 · 다른 설정 경로). 이전에서 빠뜨리기 쉬운 항목. 사용 여부를 에이전트가 확인한다. 카탈로그 기본 등급 적용",
                agent_needed=True, code_refs=[], deploy_refs=_deploy_refs(unreferenced),
                target={"env_kind": env_kind, "impl": entry.get("impl", ""), "verify": entry.get("verify", "")},
                risk="migration_omission",
            ))

    # Unmapped evidence stays visible: other-cloud imports and unknown SDK services are never dropped.
    for f in findings:
        if f.kind == "other_cloud_import" or (f.kind == "sdk_call" and f.role == "unknown"):
            n += 1
            items.append(Item(f"item-{n:03d}", "unknown", f.service, "code", "unknown", "rule", "카탈로그 밖의 SDK/클라우드 사용. 에이전트가 분류하고 이유를 쓴다", True, _code_refs([f]), [], {"env_kind": env_kind}))
    return items
