"""Cloudflare Access tokens for the web console: only the team's key, issuer and app, unexpired, for a mapped email."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sova_mcp.access import AccessVerifier

TEAM = "bravo.cloudflareaccess.com"
AUD = "a" * 64
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def __init__(self, key=KEY, down=False):
        self.key, self.down = key, down

    def get_signing_key_from_jwt(self, _token):
        if self.down:
            raise jwt.PyJWKClientConnectionError("unreachable")
        return SimpleNamespace(key=self.key.public_key())


def token(key=KEY, **claims):
    now = int(time.time())
    body = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 600, "email": "Person@Example.com",
            "sub": "user-1"} | claims
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})


def verifier(**kw):
    return AccessVerifier(TEAM, AUD, {"person@example.com": {"tenant_id": "bravo", "subject_id": "sso-person"}}, jwks=Keys(**kw))


def ask(v, value):
    return v(SimpleNamespace(headers={"authorization": value}))


def test_accepts_the_team_token_for_a_mapped_person_as_their_existing_subject():
    actor = ask(verifier(), "Bearer cfaccess." + token())
    assert (actor.subject_id, actor.tenant_id) == ("sso-person", "bravo") and actor.expires_at > time.time()


@pytest.mark.parametrize("bad", [
    token(aud=["another-app"]), token(iss="https://other.cloudflareaccess.com"), token(exp=int(time.time()) - 120),
    token(email="stranger@example.com"), token(email=None), token(key=OTHER), "not-a-jwt",
])
def test_refuses_other_apps_issuers_expired_unmapped_and_forged(bad):
    assert ask(verifier(), "Bearer cfaccess." + bad) is None


def test_other_schemes_are_not_access_and_key_outage_is_unavailable_not_refused():
    assert ask(verifier(), "Bearer " + token()) is None
    with pytest.raises(RuntimeError, match="access_keys_unavailable"):
        ask(verifier(down=True), "Bearer cfaccess." + token())


def test_configuration_is_checked():
    with pytest.raises(ValueError):
        AccessVerifier("evil.example.com", AUD, {"a@b.c": {"tenant_id": "t", "subject_id": "s"}}, jwks=Keys())
    with pytest.raises(ValueError):
        AccessVerifier(TEAM, AUD, {"a@b.c": {"tenant_id": "t"}}, jwks=Keys())
