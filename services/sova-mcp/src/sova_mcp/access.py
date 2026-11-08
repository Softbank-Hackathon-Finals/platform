"""Cloudflare Access identity for the web console (sova.mangowhois.ai).

The console server forwards the person's own Access token as ``Bearer cfaccess.<jwt>``. It is accepted only when
signed by the team's published keys, issued by that team, for the console's Access application, unexpired, and for
an email this host maps to that person's existing SOVA subject. The same tenant grants then apply as for their AWS
SSO proof, and web actions are recorded under the same subject. The mapping lives in the runtime config Secret.
"""

import time

import jwt
from bravo_platform_events.registration_sso import SsoRegistrationPrincipal

PREFIX = "cfaccess."


class AccessVerifier:
    def __init__(self, team_domain: str, aud: str, identities: dict, jwks=None, clock=time.time):
        if not team_domain.endswith(".cloudflareaccess.com") or not aud or not identities:
            raise ValueError("invalid_access_configuration")
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        self.identities = {email.lower(): row for email, row in identities.items()}
        if any(set(row) != {"tenant_id", "subject_id"} for row in self.identities.values()):
            raise ValueError("invalid_access_identity")
        self.jwks = jwks or jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=5)
        self.clock = clock

    def __call__(self, request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer " + PREFIX):
            return None
        token = header.removeprefix("Bearer " + PREFIX)
        if len(token) > 8192:
            return None
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer, leeway=30,
                                options={"require": ["exp", "iat", "email"]})
        except jwt.PyJWKClientConnectionError:
            raise RuntimeError("access_keys_unavailable") from None  # → 503 identity_unavailable, not a refusal
        except jwt.PyJWTError:
            return None
        row = self.identities.get(str(claims["email"]).lower())
        if row is None:
            return None
        return SsoRegistrationPrincipal(row["subject_id"], row["tenant_id"], int(claims["iat"]), int(claims["exp"]),
                                        "access:" + str(claims.get("sub", ""))[:64])
