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

# AWS_ENDPOINT_URL_<SERVICE> (botocore service-specific endpoint) -> boto3 service name. The
# suffix is botocore's service id upper-cased with spaces as underscores, which is not always the
# client name ("Secrets Manager" -> SECRETS_MANAGER, "CloudWatch Logs" -> CLOUDWATCH_LOGS).
ENDPOINT_ENV_PREFIX: Final[str] = "AWS_ENDPOINT_URL_"
ENDPOINT_ENV_SERVICE: Final[dict[str, str]] = {
    "S3": "s3",
    "SECRETS_MANAGER": "secretsmanager",
    "SSM": "ssm",
    "KMS": "kms",
    "IAM": "iam",
    "STS": "sts",
    "RDS": "rds",
    "DYNAMODB": "dynamodb",
    "SQS": "sqs",
    "SNS": "sns",
    "CLOUDWATCH": "cloudwatch",
    "CLOUDWATCH_LOGS": "logs",
    "CLOUDTRAIL": "cloudtrail",
    "EKS": "eks",
    "ECR": "ecr",
    "LAMBDA": "lambda",
    "BEDROCK_RUNTIME": "bedrock-runtime",
    "BEDROCK": "bedrock",
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
        # T46 (ADR-0045 · 0046): SQS 표준 큐 → ElasticMQ, SQS 이벤트 소스 매핑으로 실행되는 Lambda
        # 핸들러 → 호환 런타임. 범위 밖(FIFO · 필터 · 배치 일부 실패 응답 · SQS 외 트리거 · Lambda API
        # 직접 호출)은 grading이 'none'으로 남긴다. 같은 역할의 SNS는 SERVICE_OVERRIDES가 'none'으로 둔다.
        "messaging": {
            "impl": "elasticmq",
            "grade": "config_swap",
            "verify": "boto3 SQS 송수신 · 삭제 · 재전달 · 실패 보관 큐 · 재시작 뒤 보존, 허용 목록 밖 Pod의 큐 접근 거부(NetworkPolicy) · 평문 포트 접속 거부 · 전송 암호화 (ADR-0046)",
        },
        "function": {
            "impl": "function-runtime",
            "grade": "adapter",
            "verify": "리허설 검증 Job 채점(기대 결과 · 재시도 · 실패 보관 · 중복 1건), 출발지 이벤트 소스 매핑 · 함수 설정 대조, 목적지 AWS 송신 · 자격 차단 (ADR-0045)",
        },
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

# Statically visible features outside the T46 compatible runtime (ADR-0045: SQS 표준 큐 → Python
# 핸들러, SQS 이벤트 소스 매핑). feature -> (role, service, label). Any of them keeps the role 'none'.
SCOPE_FEATURES: Final[dict[str, tuple[str, str, str]]] = {
    "sqs_fifo": ("messaging", "sqs", "FIFO 큐"),
    "batch_item_failures": ("function", "lambda", "배치 일부 실패 응답(ReportBatchItemFailures)"),
    "event_filter": ("function", "lambda", "이벤트 필터"),
    "non_sqs_source": ("function", "lambda", "SQS 외 이벤트 소스(스트림 · Kafka · MQ · DocumentDB)"),
}
# Terraform resources that wire a Lambda to something other than an SQS event source mapping.
NON_SQS_TRIGGER_RESOURCES: Final[dict[str, str]] = {
    "aws_lambda_permission": "푸시 트리거 권한(API Gateway · S3 · SNS · EventBridge 등)",
    "aws_lambda_function_url": "함수 URL",
}

# Same role, different answer per service. A role entry must not speak for a service its target
# cannot stand in for: ElasticMQ offers the SQS API only, so SNS stays 'none' under `messaging`.
SERVICE_OVERRIDES: Final[dict[str, dict[str, dict[str, str]]]] = {
    "onprem": {
        "sns": {"impl": "", "grade": "none", "verify": "대체 없음: ElasticMQ는 SQS API만 제공(SNS 토픽 · 구독 없음). 브로커 선택 필요"},
    },
}

GRADES: Final[tuple[str, ...]] = ("config_swap", "adapter", "code_change", "none", "unknown")
# One item covering several services takes the most demanding grade (ADR-0018 옵션 3).
GRADE_WEIGHT: Final[dict[str, int]] = {"config_swap": 0, "adapter": 1, "code_change": 2, "unknown": 3, "none": 4}


def role_for_service(service: str) -> str:
    return SERVICE_ROLE.get(service.lower(), "unknown")


def service_for_endpoint_env(name: str) -> str | None:
    """boto3 service of an ``AWS_ENDPOINT_URL_<SERVICE>`` variable. ``None`` for any other name
    (including the global ``AWS_ENDPOINT_URL``) and for services the catalog does not know."""
    if not name.startswith(ENDPOINT_ENV_PREFIX):
        return None
    return ENDPOINT_ENV_SERVICE.get(name.removeprefix(ENDPOINT_ENV_PREFIX))


def service_entries(env_kind: str, role: str, services: set[str]) -> dict[str, dict[str, str]]:
    """Target entry per service of one role: the service override when there is one, the role
    entry otherwise. Empty when the role has no entry at this target."""
    entry = CATALOG.get(env_kind, {}).get(role)
    if entry is None:
        return {}
    overrides = SERVICE_OVERRIDES.get(env_kind, {})
    return {s: overrides.get(s, entry) for s in sorted(services)}


def target_entry(env_kind: str, role: str, services: set[str] | None = None) -> dict[str, str] | None:
    """The role's target entry, or the most demanding per-service entry when the item's services
    are known (an item with SNS and SQS is not represented by ElasticMQ)."""
    entry = CATALOG.get(env_kind, {}).get(role)
    if entry is None or not services:
        return entry
    return max(service_entries(env_kind, role, services).values(), key=lambda e: GRADE_WEIGHT[e["grade"]])
