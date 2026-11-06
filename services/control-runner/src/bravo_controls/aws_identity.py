"""Build bounded AWS clients from one temporary credential snapshot and verify it.

This is a trusted worker boundary, not an endpoint accepting credentials or role
names from a browser. Registration owns the expected IAM RoleId as well as ARN;
credential issuance and permission policy remain outside this module.
"""

import hashlib
import logging
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from botocore import UNSIGNED
from botocore.config import Config
from botocore.session import Session

_ROLE = re.compile(r"arn:(aws|aws-cn|aws-us-gov):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})\Z")
_ROLE_ID = re.compile(r"AROA[A-Z0-9]{17}\Z")
_SESSION = re.compile(r"[A-Za-z0-9+=,.@_-]{2,64}\Z")
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]\Z")
MIN_REMAINING = timedelta(seconds=60)


class IdentityUnavailable(ValueError):
    """Fixed error only; never carry provider exceptions, credentials or URLs."""


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IdentityUnavailable("AWS_IDENTITY_TIME_INVALID")
    return value.astimezone(UTC)


def _sha(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class RegisteredRole:
    arn: str
    role_id: str

    def __post_init__(self):
        if (not isinstance(self.arn, str) or not _ROLE.fullmatch(self.arn)
                or not isinstance(self.role_id, str) or not _ROLE_ID.fullmatch(self.role_id)
                or any(not part for part in self.arn.split(":role/", 1)[1].split("/"))):
            raise IdentityUnavailable("REGISTERED_AWS_ROLE_REQUIRED")


@dataclass(frozen=True)
class TemporaryCredentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self):
        if (not isinstance(self.access_key_id, str)
                or not re.fullmatch(r"ASIA[A-Z0-9]{16}", self.access_key_id)
                or not isinstance(self.secret_access_key, str)
                or not 1 <= len(self.secret_access_key) <= 4096
                or not isinstance(self.session_token, str)
                or not 1 <= len(self.session_token) <= 32768):
            raise IdentityUnavailable("TEMPORARY_AWS_CREDENTIALS_REQUIRED")
        _utc(self.expires_at)


def validate_identity(role, response):
    """Reject IAM users, another account/role, and a recreated role with the same name."""
    if not isinstance(role, RegisteredRole) or type(response) is not dict:
        raise IdentityUnavailable("AWS_IDENTITY_MISMATCH")
    partition, account, path = _ROLE.fullmatch(role.arn).groups()
    prefix = f"arn:{partition}:sts::{account}:assumed-role/{path.rsplit('/', 1)[-1]}/"
    arn = response.get("Arn")
    if not isinstance(arn, str) or not arn.startswith(prefix):
        raise IdentityUnavailable("AWS_IDENTITY_MISMATCH")
    session = arn[len(prefix):]
    metadata = response.get("ResponseMetadata")
    if (not _SESSION.fullmatch(session) or response.get("Account") != account
            or response.get("UserId") != f"{role.role_id}:{session}"
            or type(metadata) is not dict or metadata.get("HTTPStatusCode") != 200):
        raise IdentityUnavailable("AWS_IDENTITY_MISMATCH")
    return {"role_arn_sha256": _sha(role.arn), "role_id_sha256": _sha(role.role_id),
            "session_identity_sha256": _sha(arn + "\n" + response["UserId"])}


def _client(session, service, region, credentials=None):
    if service not in {"sts", "secretsmanager", "s3", "kms", "logs"}:
        raise IdentityUnavailable("AWS_SERVICE_NOT_QUALIFIED")
    kwargs = ({"aws_access_key_id": credentials.access_key_id,
               "aws_secret_access_key": credentials.secret_access_key,
               "aws_session_token": credentials.session_token} if credentials else
              {"aws_access_key_id": "", "aws_secret_access_key": "", "aws_session_token": ""})
    # Explicit verification and ignored configured endpoints prevent environment
    # URL / CA overrides from turning a successful fake STS response into identity.
    # This first path uses AWS regional endpoints, not custom proxy/VPCE endpoints.
    return session.create_client(service, region_name=region, verify=True, **kwargs,
                                 config=Config(
                                     signature_version=("s3v4" if service == "s3" else "v4") if credentials else UNSIGNED,
                                     connect_timeout=3, read_timeout=5,
                                     retries={"mode": "standard", "total_max_attempts": 1},
                                     proxies={}, ignore_configured_endpoint_urls=True,
                                     use_fips_endpoint=False, use_dualstack_endpoint=False,
                                 ))


def _require_no_wire_debug():
    names = {"botocore", "botocore.endpoint", "botocore.auth", "botocore.parsers",
             "urllib3", "urllib3.connectionpool"}
    names.update(name for name in logging.Logger.manager.loggerDict
                 if name.startswith(("botocore.", "urllib3.")))
    if any(logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in names):
        raise IdentityUnavailable("AWS_WIRE_DEBUG_LOGGING_FORBIDDEN")


@contextmanager
def verified_client(role, credentials, service, region, *, clock=lambda: datetime.now(UTC)):
    """Yield (client, identity evidence); caller retains evidence before accepting PASS.

Both STS and service use the exact explicit snapshot, never ambient credentials.
The host must supervise the complete job deadline and keep SDK wire logs disabled.
GetCallerIdentity verifies identity, not effective authorization or non-revocation.
The actual positive/negative service requests establish the access observation.
"""
    if (not isinstance(role, RegisteredRole) or not isinstance(credentials, TemporaryCredentials)
            or service not in {"s3", "secretsmanager", "kms", "logs"}
            or not isinstance(region, str) or not _REGION.fullmatch(region)):
        raise IdentityUnavailable("AWS_CLIENT_BINDING_INVALID")
    before = _utc(clock())
    expiry = _utc(credentials.expires_at)
    if expiry - before < MIN_REMAINING:
        raise IdentityUnavailable("AWS_CREDENTIALS_TOO_CLOSE_TO_EXPIRY")
    # Resolve only SDK-owned endpoints in this trusted worker. No provider client
    # supplied by an uploaded application is accepted here.
    _require_no_wire_debug()
    try:
        session = Session()
        partition = session.get_partition_for_region(region)
    except Exception:
        raise IdentityUnavailable("AWS_CLIENT_UNAVAILABLE") from None
    if partition != _ROLE.fullmatch(role.arn).group(1):
        raise IdentityUnavailable("AWS_PARTITION_MISMATCH")
    with ExitStack() as stack:
        try:
            sts = _client(session, "sts", region, credentials)
            stack.callback(sts.close)
            response = sts.get_caller_identity()
            evidence = validate_identity(role, response)
            checked = _utc(clock())
            if checked < before or expiry - checked < MIN_REMAINING:
                raise IdentityUnavailable("AWS_IDENTITY_EXPIRED_OR_CLOCK_CHANGED")
            client = _client(session, service, region, credentials)
            stack.callback(client.close)
        except IdentityUnavailable:
            raise
        except Exception:
            raise IdentityUnavailable("AWS_IDENTITY_UNAVAILABLE") from None
        evidence.update(verified_at=checked.isoformat(), expires_at=expiry.isoformat())
        yield client, evidence


@contextmanager
def anonymous_s3_client(region):
    """No credential lookup and no caller-selected endpoint for anonymous S3 checks."""
    if not isinstance(region, str) or not _REGION.fullmatch(region):
        raise IdentityUnavailable("AWS_CLIENT_BINDING_INVALID")
    _require_no_wire_debug()
    try:
        client = _client(Session(), "s3", region)
    except Exception:
        raise IdentityUnavailable("AWS_CLIENT_UNAVAILABLE") from None
    try:
        yield client
    finally:
        client.close()
