"""Local identity provider for the isolated E2E: RS256 RFC 9068 access tokens for named subjects.

T10 and T45 verify people with `OperatorTokenVerifier` (pinned public JWKS, subjects, 300 s tokens).
This stands in for the team IdP only; the verifier, the grants and every permission check are the real
code. The private key stays in the E2E state directory (0600) and never leaves this host.
"""

import json
import os
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.bravo-e2e.invalid"
AUDIENCE = "bravo-platform-operator"
CLIENT_ID = "bravo-e2e"
ASSURANCE = "bravo-e2e-local"
KID = "bravo-e2e-1"


class LocalIdp:
    def __init__(self, state: str | Path):
        self.path = Path(state) / "idp-key.pem"
        if not self.path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        self.key = serialization.load_pem_private_key(self.path.read_bytes(), password=None)

    def jwks(self) -> dict:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [public | {"kid": KID, "alg": "RS256", "use": "sig"}]}

    def verifier_config(self, subjects: dict[str, str]) -> dict:
        """Keyword arguments for OperatorTokenVerifier: subject -> tenant."""
        return {"issuer": ISSUER, "audience": AUDIENCE, "client_id": CLIENT_ID, "assurance": ASSURANCE,
                "subjects": subjects, "public_jwks": self.jwks()}

    def token(self, subject: str, *, lifetime_s: int = 290) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": subject, "aud": AUDIENCE, "iat": now, "nbf": now, "auth_time": now,
                  "exp": now + lifetime_s, "jti": str(uuid.uuid4()), "client_id": CLIENT_ID, "acr": ASSURANCE}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID, "typ": "at+jwt"})
