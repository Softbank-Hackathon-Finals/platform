"""Join code findings with the deployment graph into inventory items (design 8.11 대조).

The source kind (ADR-0034) arrives with the data: every finding carries the kind whose rules
read it and ``cloud_dependency``; the deploy graph carries the kind of its adapter. Both are
copied onto the item's references so the catalog (A4) can tell an on-prem S3-compatible store
from an AWS S3 dependency without re-scanning.

Grading: with an explicit ``source_kind`` and a catalog hub that accepts it (A4:
``grade_role(..., source_kind=)``, ``target_entry(source, target, role)``), items are graded for
the (source, target) pair here. Without it they are graded against the target default as before,
so a caller that re-grades afterwards (A4 ``apply_pair``) sees the same input.
"""

from __future__ import annotations

import inspect
from collections import defaultdict

from .catalog import target_entry
from .deploy_trace import CLOUD_RESOURCE_KINDS, ENV_NAMED_KINDS, DeployGraph
from .grading import Item, _env_names, grade_role
from .static_scan import Finding

ROLE_KINDS = {"object_store": "s3_bucket", "secrets": "secret", "kms": "kms_key", "iam": "iam_role", "db": "cnpg_cluster", "registry": "image"}
SPLIT_KINDS = ENV_NAMED_KINDS  # named by a workload setting, or deploy-only
# Only a source that is not a public cloud gets this note. On aws/gcp, evidence without a cloud
# marker (DB_HOST, a generic env name) does not show the backing service is self-run: it may be
# RDS or Cloud SQL. Saying "not a cloud dependency" there would be wrong, so those items keep the
# rationale they had before ADR-0034 (aws→onprem output = develop).
NON_CLOUD_SOURCE_KINDS = frozenset({"onprem"})
NON_CLOUD_NOTE = " 출발지({kind})는 퍼블릭 클라우드가 아니며, 이 항목의 코드 · 배포 증거에 퍼블릭 클라우드 관리형 서비스 의존이 없다(S3 호환 저장소 · OpenBao 등 자체 운영)."


def _accepts(func, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _accepts_varargs(func) -> bool:
    try:
        return any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return False


# The catalog hub (A4) grades for a (source, target) pair. Older catalogs grade for the target only.
_GRADE_TAKES_SOURCE = _accepts(grade_role, "source_kind")
_ENTRY_TAKES_SOURCE = _accepts_varargs(target_entry)


def _grade(role: str, code: list[Finding], env_names: set[str], env_kind: str, source_kind: str | None) -> tuple[str, str, bool]:
    if source_kind and _GRADE_TAKES_SOURCE:
        return grade_role(role, code, env_names, env_kind, source_kind=source_kind)
    return grade_role(role, code, env_names, env_kind)


def _entry(source_kind: str | None, env_kind: str, role: str) -> dict:
    entry = target_entry(source_kind, env_kind, role) if source_kind and _ENTRY_TAKES_SOURCE else target_entry(env_kind, role)
    return dict(entry or {})


def _code_refs(findings: list[Finding]) -> list[dict]:
    return [{"id": f.id, "kind": f.kind, "file": f.file, "line": f.line, "snippet": f.snippet, **({"detail": f.detail} if f.detail else {}),
             "source_kind": f.source_kind, "cloud_dependency": f.cloud_dependency} for f in findings]


def _cloud_resource(kind: str, source_kind: str) -> bool:
    # A wildcard grant is a cloud dependency when the identity system is a cloud's (IAM, GCP IAM).
    return kind in CLOUD_RESOURCE_KINDS or (kind == "wildcard" and source_kind in {"aws", "gcp"})


def _deploy_refs(resources, source_kind: str = "aws") -> list[dict]:
    return [{"kind": r.kind, "name": r.name, "via": r.via, "actions": r.actions, "attributes": r.attributes,
             "cloud_dependency": _cloud_resource(r.kind, source_kind)} for r in resources]


def _source_kind(findings: list[Finding], graph: DeployGraph | None) -> str:
    if graph is not None:
        return graph.source_kind
    return next((f.source_kind for f in findings), "aws")


def reconcile(findings: list[Finding], graph: DeployGraph | None, env_kind: str, source_kind: str | None = None) -> list[Item]:
    """``env_kind`` is the *target* kind (catalog). ``source_kind`` selects pair grading when
    given; the references always carry the kind recorded on the deploy graph or the findings."""
    pair_kind = source_kind
    source_kind = source_kind or _source_kind(findings, graph)
    by_role: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.kind in {"sdk_call", "env_ref", "config_env", "iac_resource"} and f.role != "unknown":
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
        grade, rationale, agent_needed = _grade(role, code, env_names, env_kind, pair_kind)
        entry = _entry(pair_kind, env_kind, role)
        item = Item(
            id=f"item-{n:03d}", role=role, service=", ".join(sorted({f.service for f in code if f.kind == "sdk_call"})) or role,
            found_in=found_in, grade=grade, grade_by="rule", rationale=rationale, agent_needed=agent_needed,
            code_refs=_code_refs(code), deploy_refs=_deploy_refs(deploy, source_kind),
            target={"env_kind": env_kind, "impl": entry.get("impl", ""), "verify": entry.get("verify", "")},
        )
        if source_kind in NON_CLOUD_SOURCE_KINDS and code and not any(f.cloud_dependency for f in code) and not any(_cloud_resource(r.kind, source_kind) for r in deploy):
            item.rationale += NON_CLOUD_NOTE.format(kind=source_kind)
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
                agent_needed=True, code_refs=[], deploy_refs=_deploy_refs(unreferenced, source_kind),
                target={"env_kind": env_kind, "impl": entry.get("impl", ""), "verify": entry.get("verify", "")},
                risk="migration_omission",
            ))

    # Unmapped evidence stays visible: other-cloud imports and unknown SDK services are never dropped.
    for f in findings:
        if f.kind == "other_cloud_import" or (f.kind == "sdk_call" and f.role == "unknown"):
            n += 1
            items.append(Item(f"item-{n:03d}", "unknown", f.service, "code", "unknown", "rule", "카탈로그 밖의 SDK/클라우드 사용. 에이전트가 분류하고 이유를 쓴다", True, _code_refs([f]), [], {"env_kind": env_kind}))
    return items
