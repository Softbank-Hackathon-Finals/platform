"""Platform Job identity at the broker: pinned keys first, node role only afterwards."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from bravo_agent import subscription_server as server
from bravo_agent.contracts import digest

KEY = generate_private_key(public_exponent=65537, key_size=2048)
OTHER = generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()
WORKLOAD = {"issuer": "https://platform-rke2.invalid", "audience": "bravo-subscription-broker",
            "subject": "system:serviceaccount:bravo-initial:initial-proposal", "public_keys": {"k1": PEM}}
SETTINGS = {"region": "ap-northeast-2", "broker_role_arn": "test-role", "workload_identity": WORKLOAD}


def token(key=KEY, kid="k1", **change):
    now = int(time.time())
    claims = {"iss": WORKLOAD["issuer"], "aud": WORKLOAD["audience"], "sub": WORKLOAD["subject"],
              "iat": now, "exp": now + 600} | change
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def sts(monkeypatch):
    calls = []

    class STS:
        def assume_role(self, **kwargs):
            calls.append(("assume_role", kwargs))
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}

        def assume_role_with_web_identity(self, **kwargs):
            calls.append(("web_identity", kwargs))
            return {"Provider": "p", "Audience": "a", "SubjectFromWebIdentityToken": "s",
                    "Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}

    monkeypatch.setattr(server.boto3, "client", lambda name, **kwargs: STS())
    return calls


def test_pinned_job_token_uses_node_role_without_sending_the_token(sts):
    access = server.authenticate_caller(SETTINGS, token())
    assert access.principal == digest({"Provider": WORKLOAD["issuer"], "Audience": WORKLOAD["audience"],
                                       "SubjectFromWebIdentityToken": WORKLOAD["subject"]})
    assert sts == [("assume_role", {"RoleArn": "test-role", "RoleSessionName": "bravo-subscription-job",
                                    "DurationSeconds": 900})]


@pytest.mark.parametrize("bad", [
    lambda: token(key=OTHER), lambda: token(kid="k2"), lambda: token(sub="system:serviceaccount:default:x"),
    lambda: token(aud="sts.amazonaws.com"), lambda: token(exp=int(time.time()) + 7200),
    lambda: token(exp=int(time.time()) - 10),
])
def test_unpinned_or_other_job_identity_never_reaches_sts(sts, bad):
    with pytest.raises((ValueError, jwt.PyJWTError)):
        server.authenticate_caller(SETTINGS, bad())
    assert sts == []


def test_actions_tokens_keep_the_existing_sts_path(sts):
    github = token(iss="https://token.actions.githubusercontent.com")
    server.authenticate_caller(SETTINGS, github)
    server.authenticate_caller({k: v for k, v in SETTINGS.items() if k != "workload_identity"}, token())
    assert [name for name, _ in sts] == ["web_identity", "web_identity"]
