"""AWS 서비스 이름을 시험할 공개 엔드포인트 주소로 바꾼다. 데이터이며 접속하지 않는다."""

# 리전별 주소가 서비스 이름과 다른 것도 있어 표로 둔다.
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


def is_known_service(service: str) -> bool:
    return service in _REGIONAL_PREFIX or service in _GLOBAL_HOST


def endpoint_host(service: str, region: str) -> str:
    if service in _GLOBAL_HOST:
        return _GLOBAL_HOST[service]
    return f"{_REGIONAL_PREFIX[service]}.{region}.amazonaws.com"


def is_global(service: str) -> bool:
    """리전이 없는 주소. 다른 계정 · 다른 리전의 목적지도 같은 주소를 쓴다."""
    return service in _GLOBAL_HOST


def bucket_host(bucket: str, region: str) -> str:
    """버킷 가상 호스트 주소. 버킷 이름은 전역에서 하나라 출발지 버킷에만 해당한다."""
    return f"{bucket}.s3.{region}.amazonaws.com"
