"""ENC-02.1: registered Secret/KMS metadata, never decrypt or rotate a secret.

Trusted host composition only. The host registers the actual reader IAM RoleId,
current Secret version and exact existing KMS key ARN. Both SDK clients verify
the same short-lived credential snapshot through STS. Hashes identify retained
evidence; they do not replace host provenance authentication in T19.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .aws_identity import RegisteredRole, TemporaryCredentials, verified_client
from .aws_probes import SecretTarget, _http
from .runner import Observation, RunContext, TestSpec

SPEC = TestSpec("ENC-02.1", "ENC-02", "1", "enabled", "not_applicable")
KEY = re.compile(r"arn:(aws|aws-cn|aws-us-gov):kms:([a-z0-9-]+):([0-9]{12}):key/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|mrk-[a-f0-9]{32})\Z")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_metadata_time_required")
    return value.astimezone(UTC)


@dataclass(frozen=True, repr=False)
class SecretEncryptionTarget:
    secret: SecretTarget
    key_arn: str

    def __post_init__(self):
        match = KEY.fullmatch(self.key_arn) if isinstance(self.key_arn, str) else None
        if not isinstance(self.secret, SecretTarget) or match is None:
            raise ValueError("registered_secret_and_existing_key_required")
        partition, region, account, _ = match.groups()
        parts = self.secret.secret_arn.split(":")
        if (parts[1], parts[3], parts[4]) != (partition, region, account):
            raise ValueError("same_region_account_key_required")

    @property
    def key_id(self):
        return self.key_arn.rsplit("/", 1)[1]

    @property
    def region(self):
        return self.key_arn.split(":")[3]


class SecretEncryptionProbe:
    def __init__(self, target, *, reader_role, credential_provider, evidence_sink,
                 clock=lambda: datetime.now(UTC), monotonic=time.monotonic):
        if (not isinstance(target, SecretEncryptionTarget)
                or not isinstance(reader_role, RegisteredRole)
                or reader_role.arn.split(":")[4] != target.secret.secret_arn.split(":")[4]
                or not all(callable(x) for x in (credential_provider, evidence_sink, clock, monotonic))):
            raise ValueError("qualified_metadata_reader_required")
        self.target, self.role = target, reader_role
        self.credentials, self.sink, self.clock, self.monotonic = credential_provider, evidence_sink, clock, monotonic

    def __call__(self, context, spec):
        target = self.target
        if (not isinstance(context, RunContext) or context.app != target.secret.app
                or context.environment != target.secret.environment or spec != SPEC
                or not isinstance(context.run_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", context.run_id) is None):
            raise ValueError("metadata_scope_or_method_mismatch")
        started = utc(self.clock())
        deadline = self.monotonic() + 60
        observed, reason, facts, identities = None, None, {}, []

        def call(client, name, **parameters):
            if self.monotonic() >= deadline:
                raise TimeoutError
            response = getattr(client, name)(**parameters)
            if self.monotonic() >= deadline:
                raise TimeoutError
            if _http(response) != 200:
                raise ValueError("metadata_response_unconfirmed")
            return response

        def reading(secret_client, kms_client):
            value = call(secret_client, "describe_secret", SecretId=target.secret.secret_arn)
            if (value.get("ARN") != target.secret.secret_arn or "DeletedDate" in value
                    or type(value.get("VersionIdsToStages")) is not dict):
                raise ValueError("secret_binding_changed")
            versions = value["VersionIdsToStages"]
            if (any(type(stages) is not list or not all(type(s) is str for s in stages)
                    for stages in versions.values())
                    or [key for key, stages in versions.items() if "AWSCURRENT" in stages] != [target.secret.version_id]):
                raise ValueError("current_secret_version_changed")
            key = call(kms_client, "describe_key", KeyId=target.key_arn).get("KeyMetadata", {})
            if (key.get("Arn") != target.key_arn or key.get("KeyId") != target.key_id
                    or key.get("AWSAccountId") != target.secret.secret_arn.split(":")[4]
                    or key.get("KeyManager") not in {"AWS", "CUSTOMER"}):
                raise ValueError("key_binding_changed")
            reference = value.get("KmsKeyId")
            default = "KmsKeyId" not in value
            if default:
                # Never DescribeKey(alias/aws/...) : a predefined alias without a
                # backing key can create an AWS managed key. List only existing aliases.
                marker, seen, found = None, set(), False
                for _ in range(5):
                    page = call(kms_client, "list_aliases", KeyId=target.key_arn, Limit=50,
                                **({"Marker": marker} if marker else {}))
                    aliases = page.get("Aliases")
                    if type(aliases) is not list or len(aliases) > 50 or type(page.get("Truncated")) is not bool:
                        raise ValueError("alias_listing_unconfirmed")
                    for alias in aliases:
                        if type(alias) is not dict:
                            raise ValueError("invalid_alias_metadata")
                        if alias.get("AliasName") == "alias/aws/secretsmanager":
                            if alias.get("TargetKeyId") != target.key_id or found:
                                raise ValueError("default_key_alias_changed")
                            found = True
                    if not page["Truncated"]:
                        break
                    marker = page.get("NextMarker")
                    if not isinstance(marker, str) or not 1 <= len(marker) <= 1024 or marker in seen:
                        raise ValueError("alias_pagination_unconfirmed")
                    seen.add(marker)
                else:
                    raise ValueError("alias_budget_exceeded")
                if not found or key["KeyManager"] != "AWS":
                    raise ValueError("default_key_unconfirmed")
            elif reference not in {target.key_arn, target.key_id}:
                # Customer aliases and cross-region/account mapping need separate qualification.
                raise ValueError("unsupported_secret_key_reference")
            if (key.get("Enabled") is not True or key.get("KeyState") != "Enabled"
                    or key.get("KeyUsage") != "ENCRYPT_DECRYPT" or key.get("KeySpec") != "SYMMETRIC_DEFAULT"):
                # Disabled/unavailable keys do not imply stored secrets are plaintext.
                raise ValueError("usable_encryption_key_unconfirmed")
            return {"current_version_matched": True, "key_matched": True,
                    "default_aws_key": default, "key_enabled": True}

        try:
            credentials = self.credentials(context, "encryption-reader", self.role)
            if not isinstance(credentials, TemporaryCredentials):
                raise ValueError("temporary_reader_required")
            with verified_client(self.role, credentials, "secretsmanager", target.region, clock=self.clock) as (secrets, secret_identity), verified_client(self.role, credentials, "kms", target.region, clock=self.clock) as (kms, kms_identity):
                identities = [secret_identity, kms_identity]
                before, after = reading(secrets, kms), reading(secrets, kms)
            finished = utc(self.clock())
            if (before != after or not started <= finished < utc(credentials.expires_at)
                    or finished - started > timedelta(seconds=60)):
                raise ValueError("metadata_or_identity_changed")
            observed, facts = "enabled", before
        except Exception as error:
            from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

            response = getattr(error, "response", None)
            detail = response.get("Error") if isinstance(response, dict) else None
            code = detail.get("Code") if isinstance(detail, dict) else None
            reason = ("TIMEOUT" if isinstance(error, (TimeoutError, ConnectTimeoutError, ReadTimeoutError))
                      else "OBSERVATION_DENIED" if isinstance(code, str) and code in {"AccessDenied", "AccessDeniedException"}
                      else "UNEXPECTED_RESPONSE" if isinstance(error, ValueError) else "TOOL_ERROR")
            finished = utc(self.clock())
        payload = encoded(dict(format_version="1", test_id=SPEC.test_id, method_version="1",
                               run_id=context.run_id, app=context.app, environment=context.environment.ref,
                               started_at=started.isoformat(), collected_at=finished.isoformat(),
                               resource_binding_sha256=digest(encoded(dict(secret=target.secret.secret_arn,
                                                                          version=target.secret.version_id, key=target.key_arn))),
                               identities=identities, observed=observed, unknown_reason=reason, facts=facts))
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "not_applicable", finished,
                           "bravo-secret-encryption", "1", digest(payload), reason)
