"""Real RSA verification of synthetic Kubernetes-shaped tokens; no live issuance."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from bravo_platform_events.intake_deployment import (
    AUDIENCE,
    ProjectedToken,
    sender_from_config,
    sender_pod_patch,
)
from bravo_platform_events.store import EventError


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def binding(key):
    config = dict(version=1, issuer="https://kubernetes.default.svc", audience=AUDIENCE,
                  namespace="bravo-rehearsal-control", service_account="rehearsal-host",
                  tenant_id="tenant-1", key_id="cluster-key-1")
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": config["key_id"], "alg": "RS256"}
    return config, {"keys": [jwk]}


def token(key, **override):
    now = int(time.time())
    claims = dict(iss="https://kubernetes.default.svc", aud=[AUDIENCE],
                  sub="system:serviceaccount:bravo-rehearsal-control:rehearsal-host",
                  iat=now, nbf=now, exp=now + 600, **{"kubernetes.io": {"pod": {"uid": "fixture-uid"}}})
    return jwt.encode(claims | override, key, algorithm="RS256", headers={"kid": "cluster-key-1"})


def test_kubernetes_shape_and_projection_match(binding, key):
    config, jwks = binding
    sender = sender_from_config(config, jwks)
    sender.authenticate(token(key))
    patch = sender_pod_patch(config, jwks, deployment="trusted-host", container="host")
    spec = patch["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "rehearsal-host"
    assert spec["volumes"][0]["projected"]["sources"] == [{"serviceAccountToken": {
        "audience": sender.audience, "expirationSeconds": 600, "path": "token"}}]
    assert spec["containers"][0]["volumeMounts"][0]["readOnly"] is True


@pytest.mark.parametrize("override", [
    {"aud": [AUDIENCE, "https://kubernetes.default.svc"]}, {"aud": "kube-api"},
    {"sub": "system:serviceaccount:bravo-rehearsal-control:rehearsal-exec"},
    {"iss": "https://wrong.invalid"}, {"exp": int(time.time()) - 1},
    {"exp": int(time.time()) + 7200},
])
def test_wrong_binding_expiry_and_default_api_token_rejected(binding, key, override):
    with pytest.raises(EventError, match="forbidden"):
        sender_from_config(*binding).authenticate(token(key, **override))


@pytest.mark.parametrize("fault", ["missing", "duplicate", "private", "encrypt", "hs", "url", "audience", "namespace"])
def test_bad_public_binding_rejected(binding, fault):
    config, jwks = binding
    public = jwks["keys"][0]
    if fault == "missing":
        config["key_id"] = "other"
    elif fault == "duplicate":
        jwks["keys"].append(dict(public))
    elif fault == "private":
        public["d"] = "PRIVATE_SENTINEL"
    elif fault == "encrypt":
        public["use"] = "enc"
    elif fault == "hs":
        public["alg"] = "HS256"
    elif fault == "url":
        public["jku"] = "https://untrusted.invalid"
    elif fault == "audience":
        config["audience"] = "https://kubernetes.default.svc"
    else:
        config["namespace"] = "../other"
    with pytest.raises(ValueError, match="^invalid_intake_deployment$"):
        sender_from_config(config, jwks)


def test_projected_token_rotation_and_safe_errors(tmp_path):
    path = tmp_path / "token"
    supplier = ProjectedToken(path)
    for value in ("first.fixture.token", "second.fixture.token"):
        path.write_text(value)
        assert supplier() == value
    for value in ("", "PRIVATE_SENTINEL\nother", "x" * 16385):
        path.write_text(value)
        with pytest.raises(ValueError, match="^projected_token_unavailable$"):
            supplier()
    path.unlink()
    with pytest.raises(ValueError, match="^projected_token_unavailable$"):
        supplier()


def test_forged_token_cannot_select_its_signing_key(binding):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(EventError, match="forbidden"):
        sender_from_config(*binding).authenticate(token(other_key))


def test_weak_pinned_key_rejected(binding):
    config, _ = binding
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    jwk = json.loads(RSAAlgorithm.to_jwk(weak.public_key())) | {"kid": config["key_id"]}
    with pytest.raises(ValueError, match="^invalid_intake_deployment$"):
        sender_from_config(config, {"keys": [jwk]})
