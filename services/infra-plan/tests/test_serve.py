"""The T45 API with T10's real operator verifier: only a verified, registered person gets in."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_executor import REQUEST, world  # noqa: F401  (fixture)

from bravo_infra_plan.adapters import FilePlanStore
from bravo_infra_plan.api import create_app
from bravo_infra_plan.serve import FIELDS, build, operator_resolver, operator_verifier

ISSUER, AUDIENCE, CLIENT, ACR = "https://idp.example.test", "bravo-infra-plan", "console", "mfa"


def keypair(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, public | {"kid": kid, "alg": "RS256", "use": "sig"}


def token(private, kid, *, sub="alice", age=0, lifetime=120):
    now = int(time.time()) - age
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "client_id": CLIENT, "acr": ACR, "jti": f"j-{now}-{sub}",
              "iat": now, "nbf": now, "auth_time": now, "exp": now + lifetime}
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid, "typ": "at+jwt"})


@pytest.fixture
def setup(world):  # noqa: F811 (fixture)
    private, public = keypair("k1")
    verifier = operator_verifier({"issuer": ISSUER, "audience": AUDIENCE, "client_id": CLIENT, "assurance": ACR,
                                  "subjects": {"alice": "tenant-demo"}, "public_jwks": {"keys": [public]}})
    seen = []

    def executor_for(bearer):
        seen.append(bearer)  # the same token goes on to T10
        return world["executor"]()

    app = create_app(stack=world["executor"]().stack, plans=FilePlanStore(world["tmp"] / "plans"),
                     executor_for=executor_for, resolve_principal=operator_resolver(verifier))
    return TestClient(app), private, seen


def test_a_verified_person_plans_and_their_own_token_is_what_t10_sees(setup):
    http, private, seen = setup
    bearer = token(private, "k1")
    response = http.post("/v1/infra/plans", json=REQUEST, headers={"Authorization": f"Bearer {bearer}"})
    assert response.status_code == 201, response.text
    assert response.json()["requested_by"].startswith("operator-")  # T10's subject id, not the raw sub
    assert seen == [bearer]


@pytest.mark.parametrize("make", [
    lambda private: token(rsa.generate_private_key(public_exponent=65537, key_size=2048), "k1"),  # forged
    lambda private: token(private, "k1", age=3600),  # expired
    lambda private: token(private, "k1", sub="mallory"),  # not a registered person
    lambda private: "not-a-token",
])
def test_anything_else_is_unauthenticated(setup, make):
    http, private, seen = setup
    response = http.post("/v1/infra/plans", json=REQUEST, headers={"Authorization": f"Bearer {make(private)}"})
    assert response.status_code == 401 and seen == []
    assert http.post("/v1/infra/plans", json=REQUEST).status_code == 401


def test_host_configuration_is_exact():
    with pytest.raises(ValueError, match="invalid_infra_plan_host_configuration"):
        build({key: "x" for key in FIELDS - {"t10_url"}} | {"version": 1})
