"""AWS 서비스 이름을 시험할 공개 엔드포인트 주소로 바꾼다. 데이터이며 접속하지 않는다."""

from .inventory_input import Dependency

# 리전별 주소가 서비스 이름과 다른 것만 적는다.
_REGIONAL_PREFIX = {
    "s3": "s3",
    "secretsmanager": "secretsmanager",
    "ssm": "ssm",
    "kms": "kms",
    "sts": "sts",
    "rds": "rds",
    "dynamodb": "dynamodb",
    "sqs": "sqs",
    "sns": "sns",
    "logs": "logs",
    "cloudwatch": "monitoring",
    "cloudtrail": "cloudtrail",
    "eks": "eks",
    "ecr": "api.ecr",
    "lambda": "lambda",
    "bedrock": "bedrock",
    "bedrock-runtime": "bedrock-runtime",
}
_GLOBAL_HOST = {"iam": "iam.amazonaws.com"}

# T16은 SDK 호출이 없으면 service에 역할 이름을 넣는다. 그때 어떤 API를 재야 하는지 정한다.
# iam은 IAM API 말고도 SDK가 자격을 받는 STS까지 함께 잰다.
# db(CNPG)는 AWS API가 아니라 명세의 출발지 경로로 잰다.
_ROLE_SERVICES = {
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


def services_for(dependency: Dependency) -> tuple[str, ...]:
    """이 의존성을 확인하려면 어떤 AWS 서비스를 재야 하는지. 모르면 빈 값이다."""
    from_role = _ROLE_SERVICES.get(dependency.role, ())
    known = tuple(s for s in dependency.services if s in _REGIONAL_PREFIX or s in _GLOBAL_HOST)
    return tuple(sorted({*from_role, *known}))


def endpoint_host(service: str, region: str) -> str:
    if service in _GLOBAL_HOST:
        return _GLOBAL_HOST[service]
    return f"{_REGIONAL_PREFIX[service]}.{region}.amazonaws.com"
