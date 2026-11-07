"""Replacement catalog (design 7.3 / 8.11, ADR-0034 slot ``target.catalog``): what a role becomes
at a target, for every (source kind, target kind) pair of aws · gcp · onprem.

The catalog is data, not logic. Grade names follow design FR-2:
  config_swap  설정 교체   adapter  어댑터 추가   code_change  코드 수정   none  대체 없음

Role hub (ADR-0034 3.2-6): a grade is looked up as
  1. same kind (aws→aws, ...) → ``config_swap``: the same service runs in another cluster/account,
     so only endpoint and account settings change;
  2. a ``(source kind, target kind, role)`` exception from ``PAIR_EXCEPTIONS``;
  3. the target kind's default entry in ``CATALOG``.
"""

from __future__ import annotations

from typing import Final

# AWS SDK service name (boto3 client/resource name) -> platform role.
SERVICE_ROLE: Final[dict[str, str]] = {
    "s3": "object_store",
    "secretsmanager": "secrets",
    "ssm": "secrets",
    "kms": "kms",
    "iam": "iam",
    "sts": "iam",
    "rds": "db",
    "dynamodb": "db_managed",
    "sqs": "messaging",
    "sns": "messaging",
    "cloudwatch": "observability",
    "logs": "audit",
    "cloudtrail": "audit",
    "eks": "runtime",
    "ecr": "registry",
    "lambda": "function",
    "bedrock-runtime": "ai",
    "bedrock": "ai",
}

# Other-cloud SDK import prefixes (flagged, never silently ignored).
OTHER_CLOUD_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("google.cloud", "gcp"),
    ("azure.", "azure"),
)

# Environment kinds (docs/contracts/environment-kinds.json) and the roles every target must answer.
KINDS: Final[tuple[str, ...]] = ("aws", "gcp", "onprem")
ROLES: Final[tuple[str, ...]] = (
    "runtime", "db", "object_store", "secrets", "kms", "iam", "audit", "policy", "registry",
    "observability", "db_managed", "messaging", "function", "ai",
)

# role -> target implementation per environment kind, with the default grade when the
# dependency is reached through the SDK directly from a source of *another* kind.
# 설계 7.3 AWS → 온프렘 대응표. Every kind lists the same roles (``ROLES``).
CATALOG: Final[dict[str, dict[str, dict[str, str]]]] = {
    "onprem": {
        "runtime": {"impl": "rke2", "grade": "config_swap", "verify": "kube-bench · 앱 기동"},
        "db": {"impl": "cnpg", "grade": "config_swap", "verify": "무결성 영수증 (복제로 이전)"},
        "object_store": {"impl": "seaweedfs", "grade": "config_swap", "verify": "무인증 접근 거부 · 버킷 권한"},
        "secrets": {"impl": "openbao+eso", "grade": "code_change", "verify": "다른 계정의 Secret 읽기 거부 · 회전"},
        "kms": {"impl": "openbao-transit", "grade": "code_change", "verify": "키 접근 분리 · 저장 암호화"},
        "iam": {"impl": "k8s-rbac+openbao+seaweedfs-policy", "grade": "adapter", "verify": "허용 · 거부 실제 시험"},
        "audit": {"impl": "rke2-audit+pgaudit", "grade": "config_swap", "verify": "정해 둔 이벤트 실제 검색"},
        "policy": {"impl": "kyverno", "grade": "config_swap", "verify": "같은 규칙 위반 시 거부"},
        "registry": {"impl": "ghcr", "grade": "config_swap", "verify": "digest 고정 pull"},
        "observability": {"impl": "otel+prometheus", "grade": "config_swap", "verify": "공통 지표 수집"},
        "db_managed": {"impl": "", "grade": "none", "verify": "대체 없음: 데이터 모델 변경 필요"},
        "messaging": {"impl": "", "grade": "none", "verify": "대체 없음: 브로커 선택 필요"},
        "function": {"impl": "", "grade": "none", "verify": "대체 없음: 컨테이너로 재포장 필요"},
        "ai": {"impl": "", "grade": "none", "verify": "P2: AI 리소스 이전 시나리오"},
    },
    "gcp": {
        "runtime": {"impl": "gke", "grade": "config_swap", "verify": "앱 기동"},
        "db": {"impl": "cnpg", "grade": "config_swap", "verify": "무결성 영수증"},
        "object_store": {"impl": "gcs-s3-interop", "grade": "config_swap", "verify": "무인증 접근 거부"},
        "secrets": {"impl": "secret-manager+eso", "grade": "code_change", "verify": "다른 계정의 Secret 읽기 거부"},
        "kms": {"impl": "cloud-kms", "grade": "code_change", "verify": "키 접근 분리"},
        "iam": {"impl": "workload-identity", "grade": "adapter", "verify": "허용 · 거부 실제 시험"},
        "audit": {"impl": "cloud-audit-logs+pgaudit", "grade": "config_swap", "verify": "이벤트 검색"},
        "policy": {"impl": "kyverno", "grade": "config_swap", "verify": "같은 규칙 위반 시 거부"},
        "registry": {"impl": "artifact-registry", "grade": "config_swap", "verify": "digest 고정 pull"},
        "observability": {"impl": "otel+prometheus", "grade": "config_swap", "verify": "공통 지표 수집"},
        "db_managed": {"impl": "", "grade": "none", "verify": "대체 없음: 데이터 모델 변경 필요(Firestore 등은 같은 종류 이전에서만 설정 교체)"},
        "messaging": {"impl": "pubsub", "grade": "code_change", "verify": "메시지 송수신 · 다른 계정 구독 거부"},
        "function": {"impl": "cloud-run-functions", "grade": "code_change", "verify": "핸들러 재포장 · 호출 시험"},
        "ai": {"impl": "vertex-ai", "grade": "code_change", "verify": "모델 호출 API 교체 · 응답 동등성 시험"},
    },
    "aws": {
        "runtime": {"impl": "eks", "grade": "config_swap", "verify": "앱 기동 · Pod Security 기준"},
        "db": {"impl": "cnpg", "grade": "config_swap", "verify": "무결성 영수증 (복제로 이전)"},
        "object_store": {"impl": "s3", "grade": "config_swap", "verify": "무인증 접근 거부 · 버킷 정책"},
        "secrets": {"impl": "secrets-manager+eso", "grade": "code_change", "verify": "다른 계정의 Secret 읽기 거부 · 회전"},
        "kms": {"impl": "kms", "grade": "code_change", "verify": "키 정책 분리 · 저장 암호화"},
        "iam": {"impl": "irsa", "grade": "adapter", "verify": "허용 · 거부 실제 시험"},
        "audit": {"impl": "cloudtrail+pgaudit", "grade": "config_swap", "verify": "정해 둔 이벤트 실제 검색"},
        "policy": {"impl": "kyverno", "grade": "config_swap", "verify": "같은 규칙 위반 시 거부"},
        "registry": {"impl": "ecr", "grade": "config_swap", "verify": "digest 고정 pull"},
        "observability": {"impl": "otel+prometheus", "grade": "config_swap", "verify": "공통 지표 수집"},
        "db_managed": {"impl": "", "grade": "none", "verify": "대체 없음: 데이터 모델 변경 필요(DynamoDB는 같은 종류 이전에서만 설정 교체)"},
        "messaging": {"impl": "sqs+sns", "grade": "code_change", "verify": "메시지 송수신 · 다른 계정 구독 거부"},
        "function": {"impl": "lambda", "grade": "code_change", "verify": "핸들러 재포장 · 호출 시험"},
        "ai": {"impl": "bedrock", "grade": "code_change", "verify": "모델 호출 API 교체 · 응답 동등성 시험"},
    },
}

# Implementation of a role when source and target are the same kind (the managed service the
# source already uses). Only roles whose cross-kind entry has no implementation need a value.
SAME_KIND_IMPL: Final[dict[str, dict[str, str]]] = {
    "aws": {"db_managed": "dynamodb"},
    "gcp": {"db_managed": "firestore"},
    "onprem": {},
}
SAME_KIND_REASON: Final[str] = "같은 종류({kind}) 간 이전: 같은 서비스가 다른 클러스터 · 계정에 있어 endpoint · 계정 설정만 교체"
SAME_KIND_VERIFY: Final[str] = "출발지와 다른 클러스터 · 계정 범위에서 같은 시험 재실행"

# (source kind, target kind, role) -> exception to the target default.
#   override=True   the exception wins over the code evidence rules too (the source behaviour is
#                   known from its kind alone);
#   override absent it only replaces the catalog default (deploy-only items, roles graded by
#                   default) and the evidence rules in grading still apply.
PAIR_EXCEPTIONS: Final[dict[tuple[str, str, str], dict[str, str | bool]]] = {
    ("onprem", "aws", "object_store"): {
        "grade": "config_swap", "override": True,
        "reason": "출발지 SeaweedFS가 S3 호환 API라 S3로 endpoint · 자격만 교체",
    },
    ("onprem", "gcp", "object_store"): {
        "grade": "config_swap", "override": True,
        "reason": "출발지 SeaweedFS가 S3 호환 API라 GCS S3 상호운용 endpoint · HMAC 자격만 교체",
    },
    ("onprem", "aws", "secrets"): {
        "grade": "config_swap",
        "reason": "출발지도 ESO 주입이라 목적지 SecretStore(Secrets Manager)만 교체",
    },
    ("onprem", "gcp", "secrets"): {
        "grade": "config_swap",
        "reason": "출발지도 ESO 주입이라 목적지 SecretStore(Secret Manager)만 교체",
    },
    ("gcp", "aws", "object_store"): {
        "grade": "code_change",
        "reason": "출발지 GCS 기본 API는 S3 API가 아니다. S3 상호운용 호출이 아니면 S3 클라이언트로 코드 수정",
    },
    ("gcp", "onprem", "object_store"): {
        "grade": "code_change",
        "reason": "출발지 GCS 기본 API는 S3 API가 아니다. S3 상호운용 호출이 아니면 SeaweedFS(S3 호환) 클라이언트로 코드 수정",
    },
}

# Rationale texts the grading rules use, per target kind. Formatted with ``impl``/``target``/
# ``verify``/``role``. Target-specific wording lives here, not in grading.py.
RATIONALE_DEFAULT: Final[dict[str, str]] = {
    "missing": "카탈로그에 {target} 목적지의 '{role}' 항목이 없다",
    "none": "{target}에 대체 구현이 없다: {verify}",
    "direct_sdk": "{impl}로 바꾸려면 SDK 직접 호출을 주입 방식으로 고쳐야 한다",
    "config_only": "설정 참조만 있어 주입 경로 교체로 충분",
    "db": "같은 엔진(CNPG). 접속 주소만 교체, 데이터는 복제로 이전",
    "iam": "IAM 역할·정책은 목적지 신원 · 정책({impl})으로 번역한다",
    "api_change": "{impl}는 호출 API가 달라 호출부를 고쳐야 한다: {verify}",
}
RATIONALE: Final[dict[str, dict[str, str]]] = {
    "onprem": {"iam": "IAM 역할·정책은 목적지 RBAC·저장소 정책으로 번역한다"},
    "aws": {"iam": "출발지 신원 · 정책은 목적지 IRSA 역할 · IAM 정책 · S3 버킷 정책으로 번역한다"},
    "gcp": {"iam": "출발지 신원 · 정책은 목적지 Workload Identity · IAM 바인딩으로 번역한다"},
}

GRADES: Final[tuple[str, ...]] = ("config_swap", "adapter", "code_change", "none", "unknown")

# Used only by callers that name neither a candidate profile nor a target kind (kept so older
# callers keep their output; new callers pass ``target_kind``).
LEGACY_DEFAULT_TARGET: Final[str] = "onprem"


def role_for_service(service: str) -> str:
    return SERVICE_ROLE.get(service.lower(), "unknown")


def rationale(target_kind: str, key: str, **values: str) -> str:
    text = RATIONALE.get(target_kind, {}).get(key) or RATIONALE_DEFAULT[key]
    return text.format(target=target_kind, **values)


def pair_entry(source_kind: str, target_kind: str, role: str) -> dict[str, str | bool] | None:
    """Catalog entry for one (source, target, role), with ``basis`` = same_kind | pair_exception |
    target_default and ``reason`` (empty for target_default)."""
    base = CATALOG.get(target_kind, {}).get(role)
    if base is None:
        return None
    out: dict[str, str | bool] = {**base, "source_kind": source_kind, "target_kind": target_kind, "basis": "target_default", "reason": ""}
    if source_kind == target_kind:
        out.update(
            impl=base["impl"] or SAME_KIND_IMPL.get(target_kind, {}).get(role) or "출발지와 같은 구현",
            grade="config_swap", basis="same_kind", override=True,
            reason=SAME_KIND_REASON.format(kind=target_kind),
            verify=base["verify"] if base["grade"] != "none" else SAME_KIND_VERIFY,
        )
        return out
    exc = PAIR_EXCEPTIONS.get((source_kind, target_kind, role))
    if exc:
        out.update(exc, basis="pair_exception")
    return out


def target_entry(*args: str) -> dict | None:
    """``target_entry(source_kind, target_kind, role)`` → the pair entry (see ``pair_entry``).

    ``target_entry(env_kind, role)`` (legacy, two arguments) → the target default entry.
    """
    if len(args) == 3:
        return pair_entry(*args)
    if len(args) == 2:
        env_kind, role = args
        return CATALOG.get(env_kind, {}).get(role)
    raise TypeError("target_entry(source_kind, target_kind, role) or target_entry(env_kind, role)")
