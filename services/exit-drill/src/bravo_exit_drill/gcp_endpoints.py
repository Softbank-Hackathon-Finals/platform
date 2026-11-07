"""Google API 이름을 시험할 공개 엔드포인트 주소로 바꾼다. 데이터이며 접속하지 않는다."""

# 시험할 수 있는 Google API. 이름이 곧 `<이름>.googleapis.com` 주소다.
_SERVICES = {
    "storage",
    "secretmanager",
    "cloudkms",
    "logging",
    "monitoring",
    "iamcredentials",
    "sts",
    "iam",
    "container",
    "artifactregistry",
    "pubsub",
    "cloudfunctions",
    "aiplatform",
    "firestore",
}
# SDK 패키지나 흔한 줄임말로 적힌 이름.
_ALIASES = {
    "gcs": "storage",
    "google.cloud.storage": "storage",
    "secret_manager": "secretmanager",
    "google.cloud.secretmanager": "secretmanager",
    "kms": "cloudkms",
    "google.cloud.kms": "cloudkms",
    "google.cloud.logging": "logging",
    "google.cloud.pubsub": "pubsub",
    "google.cloud.firestore": "firestore",
}
# 리전 엔드포인트(`<이름>.<리전>.rep.googleapis.com`)가 있는 API. 출발지 리전에만 해당하는 주소다.
_REGIONAL = {"storage", "secretmanager", "cloudkms", "logging", "pubsub"}


def canonical_service(name: str) -> str:
    """알려진 이름이면 API 이름, 아니면 빈 글자."""
    if name in _SERVICES:
        return name
    return _ALIASES.get(name, "")


def global_host(service: str) -> str:
    return f"{service}.googleapis.com"


def has_regional_endpoint(service: str) -> bool:
    return service in _REGIONAL


def regional_host(service: str, region: str) -> str:
    return f"{service}.{region}.rep.googleapis.com"


def bucket_host(bucket: str) -> str:
    """GCS 버킷 가상 호스트 주소. 버킷 이름은 전역에서 하나라 출발지 버킷에만 해당한다."""
    return f"{bucket}.storage.googleapis.com"
