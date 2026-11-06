"""Optional console access-token verifier; no login issuer or credential minting.

The host pins the issuer, audience, console client, assurance class and PUBLIC
keys from its actual IdP. Tokens never choose a URL, tenant, role or algorithm.
Unconfigured deployments keep create_cutover_app's original deny-all resolver.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import jwt

from .contract import validate_identifier
from .plan_api import Principal


def operator_id(issuer: str, subject: str) -> str:
    return "operator-" + hashlib.sha256(json.dumps([issuer, subject]).encode()).hexdigest()


@dataclass(frozen=True)
class OperatorPrincipal(Principal):
    issued_at: int
    expires_at: int
    authenticated_at: int
    session_id: str


class OperatorTokenVerifier:
    """RS256 RFC9068 access tokens with an explicit, trusted human registry.

    Registry maps exact IdP subjects to server-owned tenant IDs. Only subjects
    confirmed by the operator as human accounts may be installed here. This is
    not an auto-enrolment rule based on email, token roles or token tenant claims.
    Keys are public JWKs loaded by the host; rotation/removal is host configuration.
    """

    def __init__(self, *, issuer: str, audience: str, client_id: str, assurance: str,
                 subjects: dict[str, str], public_jwks: dict, clock=None,
                 max_token_seconds: int = 300, max_auth_age_seconds: int = 900):
        url = urlsplit(issuer)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not all((audience, client_id, assurance))):
            raise ValueError("invalid_operator_issuer_configuration")
        if (type(max_token_seconds) is not int or not 1 <= max_token_seconds <= 900
                or type(max_auth_age_seconds) is not int or not 1 <= max_auth_age_seconds <= 3600):
            raise ValueError("invalid_operator_token_lifetime")
        self.issuer, self.audience = issuer, audience
        self.client_id, self.assurance = client_id, assurance
        self.max_token_seconds, self.max_auth_age_seconds = max_token_seconds, max_auth_age_seconds
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())
        self.subjects = dict(subjects)
        for subject, tenant in self.subjects.items():
            if not isinstance(subject, str) or not 1 <= len(subject) <= 512:
                raise ValueError("invalid_operator_subject")
            validate_identifier(tenant)
        self.keys = {}
        for value in public_jwks.get("keys", []):
            if (not isinstance(value, dict) or value.get("kty") != "RSA"
                    or value.get("alg") != "RS256" or value.get("use") != "sig"
                    or not isinstance(value.get("kid"), str) or not 1 <= len(value["kid"]) <= 128
                    or value["kid"] in self.keys or "d" in value
                    or value.get("key_ops", ["verify"]) != ["verify"]):
                raise ValueError("invalid_operator_public_key")
            key = jwt.PyJWK.from_dict(value, algorithm="RS256").key
            if key.key_size < 2048:
                raise ValueError("operator_key_too_small")
            self.keys[value["kid"]] = key
        if not self.keys or not self.subjects:
            raise ValueError("operator_configuration_empty")

    def verify(self, token: str) -> OperatorPrincipal | None:
        """Return no identity on failure; never log/return token or IdP diagnostics."""
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                return None
            header = jwt.get_unverified_header(token)
            if (header.get("alg") != "RS256" or header.get("typ") not in ("at+jwt", "application/at+jwt")
                    or set(header) - {"alg", "typ", "kid"}):
                return None
            key = self.keys[header["kid"]]
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                audience=self.audience, options={
                    "require": ["iss", "sub", "aud", "exp", "iat", "nbf", "auth_time", "jti", "client_id", "acr"],
                    "strict_aud": True,
                    # Explicit clock below is also used inside the plan lock.
                    "verify_exp": False, "verify_iat": False, "verify_nbf": False,
                })
            now = self.clock()
            if (any(type(claims[k]) is not int for k in ("iat", "exp", "nbf", "auth_time"))
                    or not claims["auth_time"] <= claims["iat"] <= claims["nbf"] <= now < claims["exp"]
                    or claims["exp"] - claims["iat"] > self.max_token_seconds
                    or now - claims["auth_time"] >= self.max_auth_age_seconds
                    or claims["client_id"] != self.client_id or claims["acr"] != self.assurance
                    or not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 512):
                return None
            tenant = self.subjects[claims["sub"]]
            return OperatorPrincipal(operator_id(self.issuer, claims["sub"]), tenant,
                claims["iat"], min(claims["exp"], claims["auth_time"] + self.max_auth_age_seconds), claims["auth_time"],
                hashlib.sha256(json.dumps([self.issuer, claims["jti"]]).encode()).hexdigest())
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None

    def __call__(self, request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return None
        scheme, _, token = headers[0].partition(" ")
        if scheme.lower() != "bearer" or not token or token.strip() != token:
            return None
        return self.verify(token)


OPERATOR_SSO_AUDIENCE = "bravo-platform-operator"


class OperatorSsoVerifier(OperatorTokenVerifier):
    """Human operators by the team's AWS IAM Identity Center (MFA) proof, as registration and rehearsal do.

    The same reviewed STS GetCallerIdentity proof (registration_sso), signed for a distinct operator
    audience so a registration or rehearsal proof is never an operator credential. The pinned SSO
    identity maps to its registration subject, the same subject OperatorAccess grants name.
    """

    def __init__(self, identity):  # noqa: D107 - no OIDC issuer: the AWS proof is the credential
        from .registration_sso import SsoRegistrationVerifier

        # One pinned identity per person, so approvers and executors stay distinct subjects.
        identities = list(identity) if isinstance(identity, (list, tuple)) else [identity]
        if (not identities or any(type(i) is not SsoRegistrationVerifier or i.audience != OPERATOR_SSO_AUDIENCE
                                  for i in identities)
                or len({i.subject_id for i in identities}) != len(identities)):
            raise ValueError("operator_sso_identity_required")
        self.identities = identities
        # Note: authenticated_at is the proof's issue time, which can be later than the SSO (MFA) login.

    def verify(self, token):
        # ponytail: one STS check per pinned person (a handful); match once if the team list grows.
        actor = next((a for a in (i.verify(token) for i in self.identities) if a is not None), None)
        if actor is None:
            return None
        return OperatorPrincipal(actor.subject_id, actor.tenant_id, actor.issued_at, actor.expires_at,
                                 actor.issued_at, actor.session_id)
