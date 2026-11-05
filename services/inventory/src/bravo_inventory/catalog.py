"""Replacement catalog (design 7.3 / 8.11): what a cloud-managed role becomes at a target.

The catalog is data, not logic. Grade names follow design FR-2:
  config_swap  설정 교체   adapter  어댑터 추가   code_change  코드 수정   none  대체 없음
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

# role -> target implementation per environment kind, with the default grade when the
# dependency is reached through the SDK directly. 설계 7.3 AWS → 온프렘 대응표.
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
    },
}

GRADES: Final[tuple[str, ...]] = ("config_swap", "adapter", "code_change", "none", "unknown")


def role_for_service(service: str) -> str:
    return SERVICE_ROLE.get(service.lower(), "unknown")


def target_entry(env_kind: str, role: str) -> dict[str, str] | None:
    return CATALOG.get(env_kind, {}).get(role)
