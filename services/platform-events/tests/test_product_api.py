"""Synthetic Actions issuer with real RSA, T10 journals and ASGI TLS scope."""

import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_cutover import EXPIRES, NOW
from test_product_inputs import Fixture

from bravo_platform_events.product_access import ProductInputAccess
from bravo_platform_events.product_api import (
    AUDIENCE,
    ISSUER,
    PATH,
    SUBJECT,
    AuthenticatedProductReader,
    GitHubProductCaller,
    create_product_read_app,
)
from bravo_platform_events.product_inputs import ProductInputRecords
from bravo_platform_events.store import EventError
from bravo_platform_events.workload_identity import WorkloadSender


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def api(backend, key):
    f = Fixture(backend)
    caller = GitHubProductCaller(WorkloadSender(ISSUER, AUDIENCE, SUBJECT, key.public_key(),
                                               f.admission.tenant_id), "fixture-observed-provider")
    access = ProductInputAccess(backend.store, clock=lambda: f.now)
    grant = dict(tenant_id=f.admission.tenant_id, enabled=True, valid_after=NOW, expires_at=EXPIRES,
                 expected_revision=0, administration_ref="fixture-management", command_key="grant")
    access.set_grant(f.admission.plan_id, caller.principal, **grant)
    records = ProductInputRecords(backend.store, catalog=f, source=f.source,
                                  authorize=access.require, clock=lambda: f.now)
    ref = records.register(f.admission, f.value(), principal=caller.principal, expires_at=EXPIRES)
    # The service reader can read without a source producer, but cannot register.
    read_only = ProductInputRecords(backend.store, catalog=f, authorize=access.require, clock=lambda: f.now)
    reader = AuthenticatedProductReader(read_only, caller)
    return f, caller, access, grant, reader, ref


def token(key, **changes):
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=AUDIENCE, sub=SUBJECT, iat=now, nbf=now, exp=now+300,
                  repository_id="1443045223", repository_owner_id="350259663")
    return jwt.encode(claims | changes, key, algorithm="RS256")


def test_authenticated_read_uses_existing_plan_grant_and_never_dispatches(api, key):
    f, caller, access, grant, reader, ref = api
    credential = token(key)
    with TestClient(create_product_read_app(reader), base_url="https://host.invalid") as client:
        response = client.post(PATH, json=dict(principal=caller.principal, input_ref=ref["input_ref"]),
                               headers={"Authorization": "Bearer " + credential})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["input_digest"] == ref["input_digest"]
        assert credential not in response.text
        for path in ("/v1/plans", "/docs", "/openapi.json", "/internal/v1/product-inputs/grant"):
            assert client.post(path, json={}).status_code == 404
        access.set_grant(f.admission.plan_id, caller.principal,
            **(grant | dict(enabled=False, expected_revision=1, command_key="revoke")))
        assert client.post(PATH, json=dict(principal=caller.principal, input_ref=ref["input_ref"]),
            headers={"Authorization": "Bearer " + credential}).status_code == 409
    assert f.calls == [f.admission]
    with pytest.raises(EventError, match="product_source_unavailable"):
        reader.records.product_input(f.admission, f.executing_task)


@pytest.mark.parametrize("change", [dict(iss="https://other.invalid"), dict(aud="bravo-t10-rehearsal-intake"),
    dict(sub="repo:other/platform:ref:refs/heads/develop"), dict(repository_id="1"),
    dict(repository_owner_id="1"), dict(exp=1), dict(iat=True), dict(exp=2**40)])
def test_other_identity_or_expired_token_is_rejected_before_storage(api, key, change, monkeypatch):
    f, caller, _, _, reader, ref = api
    monkeypatch.setattr(reader.records, "read", lambda *_: pytest.fail("unauthenticated input lookup"))
    with TestClient(create_product_read_app(reader), base_url="https://host.invalid") as client:
        response = client.post(PATH, json=dict(principal=caller.principal, input_ref=ref["input_ref"]),
            headers={"Authorization": "Bearer " + token(key, **change)})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "forbidden"


def test_tls_duplicate_fields_oversize_and_wrong_principal_are_rejected(api, key):
    _, caller, _, _, reader, ref = api
    headers = {"Authorization": "Bearer " + token(key), "Content-Type": "application/json"}
    with TestClient(create_product_read_app(reader), base_url="http://host.invalid") as client:
        assert client.post(PATH, headers=headers | {"X-Forwarded-Proto": "https"}, json={}).status_code == 403
    with TestClient(create_product_read_app(reader), base_url="https://host.invalid") as client:
        assert client.post(PATH, headers=headers, content='{"principal":"x","principal":"y"}').status_code == 422
        assert client.post(PATH, headers=headers, content="x" * 16385).status_code == 413
        assert client.post(PATH, headers=headers,
            json=dict(principal="e" * 64, input_ref=ref["input_ref"])).status_code == 403
        assert client.post(PATH, headers=headers, json=dict(principal=caller.principal,
            input_ref=ref["input_ref"], admission={})).status_code == 422


def test_grant_expiry_scope_conflict_and_disabled_replay(api):
    f, caller, access, grant, reader, ref = api
    assert access.set_grant(f.admission.plan_id, caller.principal, **grant)["revision"] == 1
    with pytest.raises(EventError, match="command_key_conflict"):
        access.set_grant(f.admission.plan_id, caller.principal, **(grant | {"enabled": False}))
    with pytest.raises(EventError, match="forbidden"):
        access.set_grant(f.admission.plan_id, caller.principal, **(grant | {"tenant_id": "foreign"}))
    f.now = EXPIRES
    with pytest.raises(EventError, match="product_input_unavailable"):
        reader.records.read(caller.principal, ref["input_ref"])


def test_configured_tenant_and_token_after_read_are_checked(api, key, monkeypatch):
    _, caller, _, _, reader, ref = api
    foreign = AuthenticatedProductReader(reader.records, replace(caller,
        sender=replace(caller.sender, tenant_id="foreign")))
    with pytest.raises(EventError, match="forbidden"):
        foreign.read(token(key), caller.principal, ref["input_ref"])
    original = GitHubProductCaller.authenticate
    calls = []
    def expire_after_read(self, credential, principal):
        calls.append(1)
        if len(calls) == 2:
            raise EventError("forbidden")
        return original(self, credential, principal)
    monkeypatch.setattr(GitHubProductCaller, "authenticate", expire_after_read)
    with pytest.raises(EventError, match="forbidden"):
        reader.read(token(key), caller.principal, ref["input_ref"])
