"""Verify one explicitly configured workload binding using its pinned public key."""

from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .contract import validate_identifier
from .store import EventError


@dataclass(frozen=True)
class WorkloadSender:
    issuer: str
    audience: str
    subject: str
    public_key: RSAPublicKey
    tenant_id: str

    def __post_init__(self):
        url = urlsplit(self.issuer)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not self.audience or not self.subject
                or not isinstance(self.public_key, RSAPublicKey) or self.public_key.key_size < 2048):
            raise ValueError("invalid_workload_binding")
        validate_identifier(self.tenant_id)

    def authenticate(self, token):
        """Return verified claims for additional host checks, never a permission."""
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise ValueError()
            claims = jwt.decode(token, self.public_key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.audience, subject=self.subject, options={
                                    "require": ["iss", "aud", "sub", "iat", "exp"]})
            if (claims["aud"] not in (self.audience, [self.audience])
                    or any(type(claims[k]) is not int for k in ("iat", "exp"))
                    or not 0 < claims["exp"] - claims["iat"] <= 3600):
                raise ValueError()
        except (jwt.PyJWTError, TypeError, ValueError):
            raise EventError("forbidden") from None
        return claims
