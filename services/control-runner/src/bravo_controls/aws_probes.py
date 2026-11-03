"""Read-only AWS collectors using server-registered clients and resource versions.

These collectors do not create credentials, assume roles, or authenticate a caller.
The server must bind clients to independently verified IAM identities and retain
the evidence supplied to its sink. Principal labels are not identity proof. SDK
wire/debug logging must be disabled by that server: GetSecretValue returns secret
material to the SDK even though this module never inspects or serializes it.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .runner import Environment, Observation, RunContext, TestSpec

TOOL = "bravo-controls-aws"
TOOL_VERSION = "1"
_ROLE = re.compile(
    r"arn:(aws|aws-cn|aws-us-gov):iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]{1,512}\Z"
)
_SECRET = re.compile(
    r"arn:(aws|aws-cn|aws-us-gov):secretsmanager:([a-z0-9-]+):([0-9]{12}):secret:[A-Za-z0-9/_+=.@-]{1,512}\Z"
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}\Z")


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _principal(value: str, account: str, partition: str = "aws") -> None:
    match = _ROLE.fullmatch(value) if isinstance(value, str) else None
    if not match or match.groups() != (partition, account):
        raise ValueError("registered IAM role required")


def _client(client: Any, service: str, region: str, *, anonymous: bool = False) -> None:
    """Require bounded, server-preconfigured SDK clients; never alter credentials."""
    try:
        from botocore import UNSIGNED

        meta = client.meta
        config = meta.config
        if meta.service_model.service_name != service or meta.region_name != region:
            raise ValueError
        for value in (config.connect_timeout, config.read_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 10:
                raise ValueError
        retries = config.retries
        attempts = retries.get("total_max_attempts")
        if type(attempts) is not int or not 1 <= attempts <= 2 or retries.get("mode") != "standard":
            raise ValueError
        if anonymous:
            if config.signature_version is not UNSIGNED:
                raise ValueError
        elif config.signature_version not in {"v4", "s3v4"}:
            raise ValueError
    except Exception:
        raise ValueError("bounded server-registered AWS client required") from None


@dataclass(frozen=True)
class SecretTarget:
    app: str
    environment: Environment
    secret_arn: str
    version_id: str

    def __post_init__(self):
        if (
            not isinstance(self.app, str)
            or not _ID.fullmatch(self.app)
            or not isinstance(self.environment, Environment)
            or not isinstance(self.secret_arn, str)
            or not _SECRET.fullmatch(self.secret_arn)
            or not isinstance(self.version_id, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", self.version_id)
        ):
            raise ValueError("invalid registered Secret target")


@dataclass(frozen=True)
class ObjectTarget:
    app: str
    environment: Environment
    account_id: str
    region: str
    bucket: str
    key: str
    version_id: str
    partition: str = "aws"

    def __post_init__(self):
        values = (self.app, self.account_id, self.region, self.bucket, self.key, self.version_id)
        if (
            not all(isinstance(value, str) for value in values)
            or not _ID.fullmatch(self.app)
            or not isinstance(self.environment, Environment)
            or not re.fullmatch(r"[0-9]{12}", self.account_id)
            or not re.fullmatch(r"[a-z0-9-]{3,40}", self.region)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
            or not 1 <= len(self.key.encode()) <= 1024
            or any(ord(c) < 32 for c in self.key)
            or not re.fullmatch(r"[A-Za-z0-9._+/=-]{1,1024}", self.version_id)
            or self.version_id == "null"
            or self.partition not in {"aws", "aws-cn", "aws-us-gov"}
        ):
            raise ValueError("invalid registered versioned S3 target")


def _http(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return status if type(status) is int and 100 <= status <= 599 else None


def _failure(error: Exception, *, secret: bool) -> dict:
    # Never stringify exceptions or copy Error.Message, headers, request IDs or URLs.
    response = getattr(error, "response", None)
    status = _http(response)
    detail = response.get("Error") if isinstance(response, dict) else None
    code = detail.get("Code") if isinstance(detail, dict) else None
    denied = (
        isinstance(code, str)
        and code in {"AccessDenied", "AccessDeniedException"}
        and status in ({400, 403} if secret else {403})
    )
    if denied:
        return {"outcome": "deny", "http_status": status, "reason": None}
    # Botocore timeout classes are optional; use exact SDK types, not error text.
    from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

    timeout = isinstance(error, (TimeoutError, ConnectTimeoutError, ReadTimeoutError))
    return {
        "outcome": None,
        "http_status": status,
        "reason": "TIMEOUT" if timeout else "TOOL_ERROR",
    }


class _ReadProbe:
    _recheck_denial = False

    def _setup(
        self,
        target,
        app_client,
        negative_client,
        app_principal,
        negative_principal,
        evidence_sink,
        clock,
    ):
        if app_client is negative_client or not callable(evidence_sink) or not callable(clock):
            raise ValueError("independent clients and retained evidence sink required")
        self.target = target
        self.app_client = app_client
        self.negative_client = negative_client
        self._app_principal = app_principal
        self._negative_principal = negative_principal
        self._sink = evidence_sink
        self._clock = clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if (
            not isinstance(context, RunContext)
            or not isinstance(context.run_id, str)
            or not _ID.fullmatch(context.run_id)
            or context.app != self.target.app
            or context.environment != self.target.environment
            or spec
            != TestSpec(self.test_id, self.test_id.rsplit(".", 1)[0], "1", "deny", "required")
        ):
            raise ValueError("collector context or method mismatch")
        self._validate_clients()  # Recheck SDK config, including mutations since registration.
        positive = self._read(self.app_client)
        positive_ok = positive["outcome"] == "allow"
        negative = (
            self._read(self.negative_client)
            if positive_ok
            else {"outcome": None, "http_status": None, "reason": "NOT_RUN"}
        )
        postpositive = {"outcome": None, "http_status": None, "reason": "NOT_RUN"}
        if positive_ok and negative["outcome"] == "deny" and self._recheck_denial:
            postpositive = self._read(self.app_client)
            positive_ok = postpositive["outcome"] == "allow"
        reason = negative["reason"] if positive_ok else "POSITIVE_CONTROL_FAILED"
        collected = self._clock()
        if (
            not isinstance(collected, datetime)
            or collected.tzinfo is None
            or collected.utcoffset() is None
        ):
            raise ValueError("timezone-aware collector clock required")
        collected = collected.astimezone(UTC)
        payload = {
            "format_version": "1",
            "run_id": context.run_id,
            "app": context.app,
            "environment": {
                "id": context.environment.id,
                "role": context.environment.role,
                "ref": context.environment.ref,
            },
            "test_id": self.test_id,
            "method_version": "1",
            "action": self.action,
            "resource_binding_sha256": _digest(_canonical(self._resource_binding())),
            "app_principal_sha256": _digest(self._app_principal.encode()),
            "negative_principal_sha256": _digest(self._negative_principal.encode()),
            "positive": positive,
            "negative": negative,
            "postpositive": postpositive,
            "collected_at": collected.isoformat(),
            "tool": TOOL,
            "tool_version": TOOL_VERSION,
        }
        encoded = _canonical(payload)
        try:
            self._sink(encoded)
        except Exception:
            raise ValueError("collector evidence retention failed") from None
        return Observation(
            context,
            self.test_id,
            "1",
            negative["outcome"] if positive_ok else None,
            "pass" if positive_ok else "fail",
            collected,
            TOOL,
            TOOL_VERSION,
            _digest(encoded),
            reason,
        )


class SecretAccessProbe(_ReadProbe):
    """SEC-01.1: pinned AWSCURRENT version, app role then another verified role."""

    test_id = "SEC-01.1"
    action = "secretsmanager:GetSecretValue"

    def __init__(
        self,
        target: SecretTarget,
        *,
        app_client,
        other_client,
        app_principal_arn: str,
        other_principal_arn: str,
        evidence_sink: Callable[[bytes], None],
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, SecretTarget):
            raise ValueError("registered Secret target required")
        partition, self._region, account = _SECRET.fullmatch(target.secret_arn).groups()
        _principal(app_principal_arn, account, partition)
        _principal(other_principal_arn, account, partition)
        if app_principal_arn == other_principal_arn:
            raise ValueError("different registered IAM roles required")
        self._setup(
            target,
            app_client,
            other_client,
            app_principal_arn,
            other_principal_arn,
            evidence_sink,
            clock,
        )
        self._validate_clients()

    def _validate_clients(self):
        for client in (self.app_client, self.negative_client):
            _client(client, "secretsmanager", self._region)

    def _resource_binding(self):
        return {
            "secret_arn": self.target.secret_arn,
            "version_id": self.target.version_id,
            "version_stage": "AWSCURRENT",
        }

    def _read(self, client):
        try:
            response = client.get_secret_value(
                SecretId=self.target.secret_arn,
                VersionId=self.target.version_id,
                VersionStage="AWSCURRENT",
            )
        except Exception as error:
            return _failure(error, secret=True)
        status = _http(response)
        valid = (
            status == 200
            and response.get("ARN") == self.target.secret_arn
            and response.get("VersionId") == self.target.version_id
            and isinstance(response.get("VersionStages"), list)
            and "AWSCURRENT" in response["VersionStages"]
        )
        # Deliberately never access SecretString / SecretBinary, including negative success.
        return {
            "outcome": "allow" if valid else None,
            "http_status": status,
            "reason": None if valid else "UNEXPECTED_RESPONSE",
        }


class S3AnonymousReadProbe(_ReadProbe):
    """IAM-01.1: ordinary GET, with returned current version checked for drift.

    Supplying VersionId would test s3:GetObjectVersion, a different permission,
    and could incorrectly pass a bucket allowing anonymous ordinary GetObject.
    A denied anonymous GET is followed by an authorized current-version GET:
    a missing object can also produce 403 when the anonymous caller cannot list.
    This brackets the observation; it is not an atomic policy/object snapshot.
    """

    test_id = "IAM-01.1"
    action = "s3:GetObject"
    _recheck_denial = True

    def __init__(
        self,
        target: ObjectTarget,
        *,
        app_client,
        anonymous_client,
        app_principal_arn: str,
        evidence_sink: Callable[[bytes], None],
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, ObjectTarget):
            raise ValueError("registered S3 target required")
        _principal(app_principal_arn, target.account_id, target.partition)
        self._setup(
            target,
            app_client,
            anonymous_client,
            app_principal_arn,
            "anonymous",
            evidence_sink,
            clock,
        )
        self._validate_clients()

    def _validate_clients(self):
        _client(self.app_client, "s3", self.target.region)
        _client(self.negative_client, "s3", self.target.region, anonymous=True)

    def _resource_binding(self):
        return {
            "account_id": self.target.account_id,
            "bucket": self.target.bucket,
            "key": self.target.key,
            "version_id": self.target.version_id,
        }

    def _read(self, client):
        try:
            response = client.get_object(
                Bucket=self.target.bucket,
                Key=self.target.key,
                ExpectedBucketOwner=self.target.account_id,
            )
        except Exception as error:
            return _failure(error, secret=False)
        status = _http(response)
        body = response.get("Body") if isinstance(response, dict) else None
        valid = (
            status == 200
            and response.get("VersionId") == self.target.version_id
            and callable(getattr(body, "close", None))
        )
        # GET authorization is enough; never read, log, hash or persist object contents.
        if callable(getattr(body, "close", None)):
            try:
                body.close()
            except Exception:
                valid = False
        return {
            "outcome": "allow" if valid else None,
            "http_status": status,
            "reason": None if valid else "UNEXPECTED_RESPONSE",
        }
