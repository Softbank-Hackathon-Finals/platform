"""Registration-only proof from an existing SSO session, verified by AWS STS.

This does not assert MFA, SSO login time, or change/cutover approval authority.
Only the host's exact account, immutable role ID and caller session may register.
"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx

from .contract import validate_identifier
from .operator_identity import operator_id

PREFIX = "bravo-registration-v1."
AUDIENCE_HEADER = "x-bravo-registration-id"
MAX_PROOF_SECONDS = 60
MAX_BODY_BYTES = 16384
QUERY_KEYS = {"Action", "Version", "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
              "X-Amz-Expires", "X-Amz-Security-Token", "X-Amz-Signature", "X-Amz-SignedHeaders"}


@dataclass(frozen=True)
class SsoRegistrationPrincipal:
    # Deliberately not OperatorPrincipal: registration proof cannot authorize S2/S3.
    subject_id: str
    tenant_id: str
    issued_at: int
    expires_at: int
    session_id: str


def registration_subject(account, role_id, caller_arn):
    return operator_id("aws-sso-registration:" + account, role_id + ":" + caller_arn)


def _endpoint(region, audience):
    if (not isinstance(region, str) or re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region) is None
            or not isinstance(audience, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", audience) is None):
        raise ValueError("invalid_registration_sso_configuration")
    return "https://sts." + region + ".amazonaws.com/"


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class SsoRegistrationVerifier:
    def __init__(self, *, region, audience, expected_account, expected_caller_arn,
                 expected_sso_role_arn, expected_sso_role_id, tenant_id, clock=None, transport=None):
        self.endpoint = _endpoint(region, audience)
        role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/aws-reserved/sso.amazonaws.com/"
                            r"(?:[a-z]{2}-[a-z]+-\d/)?(AWSReservedSSO_[A-Za-z0-9_+=,.@-]+_[a-f0-9]{16})",
                            expected_sso_role_arn)
        caller = re.fullmatch(r"arn:aws:sts::(\d{12}):assumed-role/([^/]+)/([A-Za-z0-9_+=,.@-]{2,64})",
                              expected_caller_arn)
        if (not role or not caller or role[1] != expected_account or caller[1] != expected_account
                or caller[2] != role[2] or re.fullmatch(r"AROA[A-Z0-9]{17}", expected_sso_role_id) is None):
            raise ValueError("exact_sso_registration_identity_required")
        validate_identifier(tenant_id)
        self.region, self.audience = region, audience
        self.account, self.caller_arn = expected_account, expected_caller_arn
        self.user_id = expected_sso_role_id + ":" + caller[3]
        self.subject_id = registration_subject(expected_account, expected_sso_role_id, expected_caller_arn)
        self.tenant_id = tenant_id
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())
        self.transport = transport

    def _parse(self, token):
        if not isinstance(token, str) or len(token) > 16384 or not token.startswith(PREFIX):
            raise ValueError()
        encoded = token[len(PREFIX):]
        if not encoded or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
            raise ValueError()
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError()
        url = raw.decode("ascii")
        if any(ord(ch) < 33 or ord(ch) > 126 for ch in url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != urlsplit(self.endpoint).netloc
                or parsed.path != "/" or parsed.fragment or not url.startswith(self.endpoint + "?")):
            raise ValueError()
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
        query = dict(pairs)
        if len(pairs) != len(query) or set(query) != QUERY_KEYS or not all(query.values()):
            raise ValueError()
        if (query["Action"] != "GetCallerIdentity" or query["Version"] != "2011-06-15"
                or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256"
                or query["X-Amz-Expires"] != str(MAX_PROOF_SECONDS)
                or query["X-Amz-SignedHeaders"] != "host;" + AUDIENCE_HEADER
                or re.fullmatch(r"[a-f0-9]{64}", query["X-Amz-Signature"]) is None):
            raise ValueError()
        stamp = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if stamp.strftime("%Y%m%dT%H%M%SZ") != query["X-Amz-Date"]:
            raise ValueError()
        key, day, region, service, scope = query["X-Amz-Credential"].split("/")
        if (re.fullmatch(r"ASIA[A-Z0-9]{16}", key) is None or day != stamp.strftime("%Y%m%d")
                or region != self.region or service != "sts" or scope != "aws4_request"):
            raise ValueError()
        issued = int(stamp.timestamp())
        if not issued <= self.clock() < issued + MAX_PROOF_SECONDS:
            raise ValueError()
        return url, issued

    def verify(self, token):
        try:
            url, issued = self._parse(token)
        except (ValueError, TypeError, UnicodeError):
            return None
        try:
            # Only the validated, fixed regional STS endpoint is contacted. No
            # environment proxy, endpoint override, redirect or shared credentials.
            # Use the transport directly: Client's INFO request log contains the
            # presigned URL. This layer never emits the URL, headers or AWS body.
            request = httpx.Request("GET", url, headers={AUDIENCE_HEADER: self.audience, "Accept": "application/json"},
                                    extensions={"timeout": {k: 5.0 for k in ("connect", "read", "write", "pool")}})
            with (self.transport or httpx.HTTPTransport(trust_env=False, retries=0)) as transport:
                response = transport.handle_request(request)
                try:
                    if response.status_code in (400, 401, 403):
                        return None
                    if response.status_code != 200:
                        raise RuntimeError()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            raise RuntimeError()
                finally:
                    response.close()
            value = json.loads(body, object_pairs_hook=_unique_json)["GetCallerIdentityResponse"]["GetCallerIdentityResult"]
            if (value["Account"] != self.account or value["Arn"] != self.caller_arn
                    or value["UserId"] != self.user_id or not issued <= self.clock() < issued + MAX_PROOF_SECONDS):
                return None
        except Exception:
            raise RuntimeError("registration_identity_unavailable") from None
        return SsoRegistrationPrincipal(self.subject_id, self.tenant_id, issued,
            issued + MAX_PROOF_SECONDS, hashlib.sha256(token.encode()).hexdigest())

    def __call__(self, request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return None
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or token.strip() != token:
            return None
        return self.verify(token)


def make_registration_token(*, profile_name, region, audience):
    """In-memory proof from an explicit existing SSO profile; never print/save it."""
    from boto3 import Session
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    endpoint = _endpoint(region, audience)
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ValueError("explicit_sso_profile_required")
    # The SDK's DEBUG signer logs its canonical query, including the session
    # token. Fail before reading SSO credentials if that diagnostic is enabled.
    if logging.getLogger("botocore.auth").isEnabledFor(logging.DEBUG):
        raise RuntimeError("registration_sso_proof_unavailable")
    try:
        credentials = Session(profile_name=profile_name, region_name=region).get_credentials()
        if credentials is None or credentials.method != "sso":
            raise ValueError()
        frozen = credentials.get_frozen_credentials()
        if not frozen.token or not frozen.access_key.startswith("ASIA"):
            raise ValueError()
        request = AWSRequest(method="GET", url=endpoint + "?Action=GetCallerIdentity&Version=2011-06-15",
                             headers={AUDIENCE_HEADER: audience})
        SigV4QueryAuth(frozen, "sts", region, expires=MAX_PROOF_SECONDS).add_auth(request)
        return PREFIX + base64.urlsafe_b64encode(request.url.encode()).decode().rstrip("=")
    except Exception:
        raise RuntimeError("registration_sso_proof_unavailable") from None
