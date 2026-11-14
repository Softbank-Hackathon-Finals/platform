"""Static detection rules per source kind (ADR-0034 slot ``source.static_rules``).

The source kind decides what a piece of evidence *means*. ``boto3.client("s3",
endpoint_url=...)`` in an AWS source is an S3 dependency; in an on-prem source it is an
S3-compatible store (SeaweedFS) and not a cloud dependency. The keys of ``STATIC_RULES`` are
the kinds of ``docs/contracts/environment-kinds.json``; a kind without rules is
``UNSUPPORTED`` (never silently scanned with another kind's rules).

This module is data plus small pure functions. It has no I/O and imports nothing from the
rest of the package, so every scanner and adapter can depend on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

KINDS: Final[tuple[str, ...]] = ("aws", "gcp", "onprem")
UNSUPPORTED: Final[str] = "UNSUPPORTED"


@dataclass(frozen=True)
class Unsupported:
    """Returned by a registry when no adapter fills ``slot`` for ``kind`` (ADR-0034 3.2-5)."""

    slot: str
    kind: str
    status: str = UNSUPPORTED

    @property
    def reason(self) -> str:
        return f"{UNSUPPORTED}: no '{self.slot}' adapter is registered for source kind '{self.kind}'"


class UnsupportedKind(ValueError):
    def __init__(self, unsupported: Unsupported):
        super().__init__(unsupported.reason)
        self.unsupported = unsupported


@dataclass(frozen=True)
class EnvClass:
    """Role of a configuration variable and whether it ties the app to a public cloud."""

    role: str
    cloud_dependency: bool


# Roles that are kind-neutral by the common base (ADR-0034 3.2-3: every environment runs CNPG)
# or that the rules could not place. Evidence for them is never counted as a cloud dependency.
NEUTRAL_ROLES: Final[frozenset[str]] = frozenset({"db", "unknown"})

AWS_ENDPOINT_RE = re.compile(r"\b[a-z0-9.-]*amazonaws\.com\b")
GCP_ENDPOINT_RE = re.compile(r"\b[a-z0-9.-]*googleapis\.com\b")
AWS_REGION_RE = re.compile(r"\b(?:ap|us|eu|sa|ca|af|me|il|mx)-(?:[a-z]+)-\d\b")
GCP_REGION_RE = re.compile(r"\b(?:asia|europe|us|northamerica|southamerica|australia|me|africa)-[a-z]+\d\b")
TF_RESOURCE_RE = re.compile(r'^\s*resource\s+"([a-z0-9]+_[a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')

# AWS_ENDPOINT_URL_<SERVICE> (botocore service-specific endpoint) → role of that service.
SERVICE_ENDPOINT_ROLES: Final[dict[str, str]] = {"S3": "object_store", "SECRETS_MANAGER": "secrets", "KMS": "kms"}

# google-cloud-* client library (``google.cloud.<module>``) → platform role.
GCP_SERVICE_ROLE: Final[dict[str, str]] = {
    "storage": "object_store",
    "secretmanager": "secrets",
    "secretmanager_v1": "secrets",
    "kms": "kms",
    "kms_v1": "kms",
    "logging": "audit",
    "logging_v2": "audit",
    "audit": "audit",
    "iam": "iam",
    "iam_credentials": "iam",
    "iam_credentials_v1": "iam",
    "resourcemanager": "iam",
    "auth": "iam",
    "oauth2": "iam",
    "pubsub": "messaging",
    "pubsub_v1": "messaging",
    "tasks": "messaging",
    "firestore": "db_managed",
    "datastore": "db_managed",
    "bigtable": "db_managed",
    "spanner": "db_managed",
    "sql": "db",
    "monitoring": "observability",
    "monitoring_v3": "observability",
    "artifactregistry": "registry",
    "functions": "function",
    "aiplatform": "ai",
    "container": "runtime",
}

# Terraform ``google_<product>_...`` → platform role. Longest prefix wins.
GCP_TF_ROLE: Final[tuple[tuple[str, str], ...]] = (
    ("google_storage_bucket_iam", "iam"),
    ("google_secret_manager_secret_iam", "iam"),
    ("google_kms_crypto_key_iam", "iam"),
    ("google_kms_key_ring_iam", "iam"),
    ("google_storage", "object_store"),
    ("google_secret_manager", "secrets"),
    ("google_kms", "kms"),
    ("google_service_account", "iam"),
    ("google_project_iam", "iam"),
    ("google_iam", "iam"),
    ("google_logging", "audit"),
    ("google_container", "runtime"),
    ("google_sql", "db"),
    ("google_artifact_registry", "registry"),
    ("google_pubsub", "messaging"),
    ("google_cloudfunctions", "function"),
    ("google_firestore", "db_managed"),
    ("google_monitoring", "observability"),
)


def gcp_service_role(service: str) -> str:
    return GCP_SERVICE_ROLE.get(service.lower(), "unknown")


def gcp_tf_role(resource_type: str) -> str:
    for prefix, role in GCP_TF_ROLE:
        if resource_type.startswith(prefix):
            return role
    return "unknown"


def _cloud(role: str) -> EnvClass:
    return EnvClass(role, role not in NEUTRAL_ROLES)


# --- aws: the original T16 rules, unchanged -------------------------------------------------

AWS_CONFIG_ENV_RE = re.compile(r"^(?:AWS_|S3_|DB_SECRET|KMS_|SECRETS?_|IRSA_|EKS_)")


def _aws_code_env(name: str) -> EnvClass:
    role = "unknown"
    if name.startswith("S3_"):
        role = "object_store"
    elif name.startswith("DB_SECRET") or name.startswith("SECRET"):
        role = "secrets"
    elif name.startswith("DB_"):
        role = "db"
    elif name.startswith("KMS_"):
        role = "kms"
    elif name.startswith("AWS_"):
        role = "iam"
    return _cloud(role)


def _aws_config_env(name: str) -> EnvClass | None:
    """``AWS_SECRET_ACCESS_KEY`` is an IAM credential, not a Secrets Manager entry;
    ``AWS_ENDPOINT_URL_<SERVICE>`` follows the service it points at."""
    if not AWS_CONFIG_ENV_RE.match(name):
        return None
    if name.startswith("AWS_ENDPOINT_URL_"):
        return _cloud(SERVICE_ENDPOINT_ROLES.get(name.removeprefix("AWS_ENDPOINT_URL_"), "iam"))
    if name.startswith("AWS_"):
        return _cloud("iam")
    if name.startswith("S3_"):
        return _cloud("object_store")
    if name.startswith("KMS_"):
        return _cloud("kms")
    return _cloud("secrets" if "SECRET" in name else "iam")


# --- gcp ---------------------------------------------------------------------------------------

GCP_CONFIG_ENV_RE = re.compile(r"^(?:GOOGLE_|GCP_|GCLOUD_|GCS_|S3_|DB_SECRET|KMS_|SECRETS?_|AWS_)")


def _gcp_env(name: str) -> EnvClass:
    if name.startswith("AWS_"):
        return _cloud("iam")  # AWS credentials in a GCP source: a cross-cloud dependency
    if "GCS" in name or "BUCKET" in name or name.startswith(("S3_", "STORAGE_")):
        return _cloud("object_store")
    if "SECRET" in name:
        return _cloud("secrets")
    if "KMS" in name:
        return _cloud("kms")
    if "LOGGING" in name or "AUDIT" in name:
        return _cloud("audit")
    if name.startswith("DB_"):
        return _cloud("db")
    if name.startswith(("GOOGLE_", "GCP_", "GCLOUD_")):
        return _cloud("iam")  # GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_CLOUD_PROJECT, ...
    return _cloud("unknown")


def _gcp_config_env(name: str) -> EnvClass | None:
    return _gcp_env(name) if GCP_CONFIG_ENV_RE.match(name) else None


# --- onprem ------------------------------------------------------------------------------------

ONPREM_CONFIG_ENV_RE = re.compile(r"^(?:AWS_|S3_|DB_SECRET|KMS_|SECRETS?_|VAULT_|BAO_|OPENBAO_|GOOGLE_|GCP_)")
# Variables the S3 SDK reads for an S3-compatible store. On-prem they configure SeaweedFS (the
# access key is a SeaweedFS identity), so they are object_store settings, not IAM.
S3_COMPAT_ENV: Final[frozenset[str]] = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3",
     "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_S3_ADDRESSING_STYLE", "AWS_S3_FORCE_PATH_STYLE"}
)
S3_ENDPOINT_ENV: Final[frozenset[str]] = frozenset({"AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"})


def _onprem_env(name: str) -> EnvClass:
    if name in S3_COMPAT_ENV or name.startswith("AWS_S3_"):
        return EnvClass("object_store", False)
    if name.startswith("AWS_ENDPOINT_URL_"):
        # A service endpoint pointed somewhere custom: the call does not reach AWS.
        return EnvClass(SERVICE_ENDPOINT_ROLES.get(name.removeprefix("AWS_ENDPOINT_URL_"), "unknown"), False)
    if name.startswith("AWS_"):
        return EnvClass("iam", True)  # AWS_ROLE_ARN, AWS_PROFILE, ...: the app still reaches AWS
    if name.startswith(("GOOGLE_", "GCP_")):
        return EnvClass("iam", True)
    if name.startswith(("VAULT_", "BAO_", "OPENBAO_")):
        return EnvClass("kms" if "TRANSIT" in name else "secrets", False)
    if name.startswith("S3_"):
        return EnvClass("object_store", False)
    if name.startswith("DB_SECRET") or name.startswith("SECRET"):
        return EnvClass("secrets", False)
    if name.startswith("DB_"):
        return EnvClass("db", False)
    if name.startswith("KMS_"):
        return EnvClass("kms", False)
    return EnvClass("unknown", False)


def _onprem_config_env(name: str) -> EnvClass | None:
    return _onprem_env(name) if ONPREM_CONFIG_ENV_RE.match(name) else None


@dataclass(frozen=True)
class StaticRules:
    kind: str
    # (pattern, cloud) endpoint literals that are cloud dependencies in this source.
    endpoint_patterns: tuple[tuple[re.Pattern[str], str], ...]
    # Hard-coded region literal of the source's own cloud (``None``: not reported).
    region: tuple[re.Pattern[str], str] | None
    # Terraform provider prefixes reported as cloud resources, with the role function.
    tf_providers: tuple[tuple[str, str], ...]
    code_env: Callable[[str], EnvClass]
    config_env: Callable[[str], EnvClass | None]
    # ``google.cloud`` / ``google.auth`` are this source's own SDK (else: other-cloud import).
    gcp_sdk: bool = False
    # ``boto3`` S3 client with a custom endpoint is an S3-compatible store, not AWS.
    s3_compatible: bool = False
    # ``hvac`` (Vault/OpenBao client) is a secrets dependency of this source.
    vault_sdk: bool = False

    def endpoint_cloud(self, value: str) -> str | None:
        for pattern, cloud in self.endpoint_patterns:
            if pattern.search(value):
                return cloud
        return None


STATIC_RULES: Final[dict[str, StaticRules]] = {
    "aws": StaticRules(
        kind="aws",
        endpoint_patterns=((AWS_ENDPOINT_RE, "aws"),),
        region=(AWS_REGION_RE, "aws"),
        tf_providers=(("aws_", "aws"),),
        code_env=_aws_code_env,
        config_env=_aws_config_env,
    ),
    "gcp": StaticRules(
        kind="gcp",
        endpoint_patterns=((GCP_ENDPOINT_RE, "gcp"), (AWS_ENDPOINT_RE, "aws")),
        region=(GCP_REGION_RE, "gcp"),
        tf_providers=(("google_", "gcp"), ("aws_", "aws")),
        code_env=_gcp_env,
        config_env=_gcp_config_env,
        gcp_sdk=True,
    ),
    "onprem": StaticRules(
        kind="onprem",
        endpoint_patterns=((AWS_ENDPOINT_RE, "aws"), (GCP_ENDPOINT_RE, "gcp")),
        region=None,
        tf_providers=(("aws_", "aws"), ("google_", "gcp")),
        code_env=_onprem_env,
        config_env=_onprem_config_env,
        s3_compatible=True,
        vault_sdk=True,
    ),
}


def static_rules_for(kind: str) -> StaticRules | Unsupported:
    return STATIC_RULES.get(kind) or Unsupported("source.static_rules", kind)
