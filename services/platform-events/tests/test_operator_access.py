"""Generated IdP keys and synthetic gate/SHIFT only; never a real operator login."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Event

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_cutover import BINDING, BINDINGS, EXPIRES, NOW, SPEC, Fixture

from bravo_platform_events.cutover import CutoverService, _key, _put
from bravo_platform_events.operator_access import OperatorAccess, create_operator_cutover_app
from bravo_platform_events.operator_identity import OperatorTokenVerifier, operator_id
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.store import EventError

EPOCH = int(datetime.fromisoformat(NOW).timestamp())
ISSUER = "https://identity.example.invalid"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Identity:
    def __init__(self, key):
        self.key = key
        self.time = EPOCH + 1
        self.public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.public.update(kid="fixture", alg="RS256", use="sig")
        self.verifier = OperatorTokenVerifier(issuer=ISSUER, audience="bravo-platform",
            client_id="console", assurance="interactive-mfa", subjects={"human-1": SPEC.tenant_id,
            "human-2": SPEC.tenant_id}, public_jwks={"keys": [self.public]}, clock=lambda: self.time)

    def token(self, *, subject="human-1", remove=None, header=None, **claims):
        value = dict(iss=ISSUER, sub=subject, aud="bravo-platform", iat=EPOCH,
            nbf=EPOCH, exp=EPOCH + 300, auth_time=EPOCH, jti="fixture-session",
            client_id="console", acr="interactive-mfa") | claims
        if remove:
            value.pop(remove)
        return jwt.encode(value, self.key, algorithm="RS256",
                          headers={"kid": "fixture", "typ": "at+jwt"} | (header or {}))

    def principal(self, subject="human-1"):
        return self.verifier.verify(self.token(subject=subject))


@pytest.fixture
def identity(signing_key):
    return Identity(signing_key)


def test_signature_verification_and_server_identity_mapping(identity):
    principal = identity.verifier.verify(identity.token(tenant_id="foreign", roles=["admin"], email="untrusted"))
    assert principal.subject_id == operator_id(ISSUER, "human-1")
    assert principal.tenant_id == SPEC.tenant_id
    assert principal.expires_at == EPOCH + 300
    assert identity.verifier.verify(identity.token(subject="service-account")) is None
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert identity.verifier.verify(Identity(other).token()) is None


@pytest.mark.parametrize("changes", [
    {"iss": "https://other.invalid"}, {"aud": "other-service"}, {"aud": ["bravo-platform"]},
    {"client_id": "worker"}, {"acr": "password-only"}, {"exp": EPOCH},
    {"nbf": EPOCH + 30}, {"iat": EPOCH + 30}, {"auth_time": EPOCH + 30},
    {"auth_time": EPOCH - 901}, {"exp": EPOCH + 301}, {"exp": str(EPOCH + 300)},
    {"iat": True}, {"jti": ""}, {"sub": 42}, {"auth_time": False},
])
def test_token_claims_fail_closed(identity, changes):
    assert identity.verifier.verify(identity.token(**changes)) is None


@pytest.mark.parametrize("name", ["iss", "sub", "aud", "exp", "iat", "nbf", "auth_time", "jti", "client_id", "acr"])
def test_required_claims(identity, name):
    assert identity.verifier.verify(identity.token(remove=name)) is None


@pytest.mark.parametrize("header", [
    {"typ": "JWT"}, {"kid": "unknown"}, {"jku": "https://attacker.invalid"},
    {"jwk": {}}, {"crit": ["custom"]}, {"x5u": "http://metadata.invalid"},
])
def test_headers_cannot_choose_a_key_source(identity, header):
    assert identity.verifier.verify(identity.token(header=header)) is None


def test_unsigned_symmetric_and_malformed_tokens(identity):
    for token in ("bad", "x" * 16385, jwt.encode({"sub": "human-1"}, "", algorithm="none"),
                  jwt.encode({"sub": "human-1"}, "test-only-symmetric-key-long-enough", algorithm="HS256")):
        assert identity.verifier.verify(token) is None


def test_keys_must_be_public_and_unambiguous(identity):
    for values in ([identity.public, identity.public], [identity.public | {"d": "PRIVATE"}],
                   [identity.public | {"use": "enc"}], []):
        with pytest.raises(ValueError):
            OperatorTokenVerifier(issuer=ISSUER, audience="api", client_id="ui", assurance="mfa",
                                  subjects={"human": "tenant"}, public_jwks={"keys": values})


class SecuredFixture(Fixture):
    def __init__(self, backend, identity):
        super().__init__(backend)
        self.identity = identity
        self.now = "2026-11-03T10:00:01+00:00"
        self.access = OperatorAccess(backend.store, clock=lambda: self.now)
        self.operator = identity.principal()
        self.submitter = identity.principal("human-2")

    def grant(self, principal=None, *, revision=0, permissions=("read", "approve_cutover", "execute_cutover")):
        return self.access.set_permissions(SPEC.plan_id, principal or self.operator,
            permissions=permissions, valid_after=NOW, expires_at=EXPIRES,
            expected_revision=revision, administration_ref="fixture-admin-decision",
            command_key=f"grant-{revision}-{(principal or self.operator).subject_id}")

    def service(self):
        return CutoverService(self.backend.reopen(), self.client, bindings=BINDINGS,
            authority=self.access.authority(self.authority), check_access=self.access.check_read,
            clock=lambda: self.now)

    def approve(self, **changes):
        return self.service().approve(self.operator, SPEC.plan_id,
            **(dict(binding=BINDING, expected_version=1, command_key="approve") | changes))

    def submit(self, approval, principal=None):
        return self.service().submit(principal or self.operator, SPEC.plan_id,
                                    approval_id=approval["approval_id"], command_key="submit")


@pytest.fixture
def secured(backend, identity):
    f = SecuredFixture(backend, identity)
    yield f
    f.client.close()


def test_permissions_missing_and_plain_principal_denied(secured):
    f = secured
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    f.grant()
    f.operator = Principal(f.operator.subject_id, f.operator.tenant_id)
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    assert not f.requests


def test_separate_operator_approve_and_execute_rights(secured):
    f = secured
    f.grant(permissions=("read", "approve_cutover"))
    f.grant(f.submitter, permissions=("read", "execute_cutover"))
    approval = f.approve()
    with pytest.raises(EventError, match="forbidden"):
        f.submit(approval)
    result = f.submit(approval, f.submitter)
    assert result["outcome"] == "OBSERVED"
    assert len(f.requests) == 1
    assert f.checked_actors[-1] == (f.submitter.subject_id, f.operator.subject_id)


@pytest.mark.parametrize("fault", ["approver-revoked", "regranted", "expired-token", "expired-grant", "approval-revoked", "upstream"])
def test_changes_between_approval_and_submit_prevent_dispatch(secured, fault):
    f = secured
    f.grant()
    f.grant(f.submitter)
    approval = f.approve()
    if fault in ("approver-revoked", "regranted"):
        f.grant(revision=1, permissions=())
        if fault == "regranted":
            f.grant(revision=2)
    elif fault == "expired-token":
        f.now = "2026-11-03T10:05:00+00:00"
    elif fault == "expired-grant":
        f.access.set_permissions(SPEC.plan_id, f.operator, permissions=["read", "approve_cutover"],
            valid_after=NOW, expires_at="2026-11-03T10:00:01+00:00", expected_revision=1,
            administration_ref="fixture-admin-decision", command_key="expire")
    elif fault == "approval-revoked":
        first = f.access.revoke_approval(SPEC.plan_id, approval["approval_id"], administration_ref="fixture-revocation")
        assert f.access.revoke_approval(SPEC.plan_id, approval["approval_id"], administration_ref="fixture-revocation") == first
    else:
        f.allowed = False
    with pytest.raises(EventError):
        f.submit(approval, f.submitter)
    assert not f.requests
    assert f.backend.store.snapshot(SPEC.plan_id)["state"] == "SHIFT"


def test_authority_is_still_required_even_with_valid_identity_and_grant(secured):
    f = secured
    f.grant()
    service = CutoverService(f.backend.store, f.client, bindings=BINDINGS,
                            authority=f.access.authority(), clock=lambda: f.now)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(f.operator, SPEC.plan_id, binding=BINDING, expected_version=1, command_key="key")
    assert not f.requests


def test_grant_idempotency_revision_and_tenant_binding(secured):
    f = secured
    receipt = f.grant()
    assert f.grant() == receipt
    with pytest.raises(EventError, match="command_key_conflict"):
        f.grant(permissions=("read",))
    with pytest.raises(EventError, match="version_conflict"):
        f.grant(revision=2)
    with pytest.raises(EventError, match="forbidden"):
        f.grant(replace(f.operator, tenant_id="foreign"))
    with f.backend.reopen()._transaction() as c:
        assert f.access._grant(c, SPEC.plan_id, f.operator.subject_id)["revision"] == 1


def test_revoke_after_dispatch_does_not_claim_remote_cancellation(secured):
    f = secured
    f.grant()
    approval = f.approve()
    f.submit(approval)
    with pytest.raises(EventError, match="approval_already_consumed"):
        f.access.revoke_approval(SPEC.plan_id, approval["approval_id"], administration_ref="fixture-revoke")
    f.grant(revision=1, permissions=())
    with pytest.raises(EventError, match="forbidden"):
        f.submit(approval)
    assert len(f.requests) == 1


def test_admin_write_failure_rolls_back(secured):
    f = secured
    with f.backend.reject_insert("plan_commands"), pytest.raises(f.backend.storage_error):
        f.grant()
    with pytest.raises(EventError, match="forbidden"):
        f.approve()


def test_revocation_committed_ahead_of_waiting_submit_wins(secured):
    f = secured
    f.grant()
    approval = f.approve()
    started = Event()
    def submit():
        started.set()
        return f.submit(approval)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with f.backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
            future = pool.submit(submit)
            assert started.wait(5)
            grant = f.access._grant(c, SPEC.plan_id, f.operator.subject_id)
            _put(c, SPEC.plan_id, _key("operator-grant", f.operator.subject_id) + "-0000000000002",
                 {}, grant | {"revision": 2, "permissions": []})
        with pytest.raises(EventError, match="forbidden"):
            future.result(timeout=10)
    assert not f.requests


def test_expiration_during_upstream_read_cannot_dispatch(secured):
    f = secured
    f.grant()
    authority = f.authority
    def expire(*args, **kw):
        result = authority(*args, **kw)
        f.now = "2026-11-03T10:05:00+00:00"
        return result
    f.authority = expire
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    assert not f.requests


def test_http_real_signatures_and_current_access_protect_snapshot_sse_and_mutations(secured):
    f = secured
    app = create_operator_cutover_app(f.backend.store, f.client, bindings=BINDINGS,
        verifier=f.identity.verifier, authority=f.authority, clock=lambda: f.now)
    client = TestClient(app)
    base = f"/v1/plans/{SPEC.plan_id}"
    headers = {"Authorization": "Bearer " + f.identity.token(), "Idempotency-Key": "approve-http"}
    for path in (base, base + "/events?follow=false"):
        assert client.get(path, headers=headers).status_code == 403
    f.grant()
    assert client.get(base, headers=headers).status_code == 200
    assert client.get(base + "/events?follow=false", headers=headers).status_code == 200
    body = dict(approval_kind="cutover", binding=BINDING, expected_version=1)
    approval = client.post(base + "/approvals", json=body, headers=headers)
    assert approval.status_code == 200
    f.grant(revision=1, permissions=())
    assert client.get(base, headers=headers).status_code == 403
    assert client.get(base + "/events?follow=false", headers=headers).status_code == 403
    response = client.post(base + "/cutover", json={"approval_id": approval.json()["approval_id"]}, headers=headers)
    assert response.status_code == 403
    assert not f.requests
    assert f.identity.token() not in response.text
    assert client.get(base, headers={"Authorization": "Bearer internal-token", "X-Tenant-ID": SPEC.tenant_id,
                                   "X-Actor-ID": f.operator.subject_id}).status_code == 403
    assert client.get(base, headers=[("Authorization", headers["Authorization"]),
                                     ("Authorization", headers["Authorization"])]).status_code == 403


def test_grant_cutoff_requires_new_authentication_not_only_token_refresh(secured):
    f = secured
    f.access.set_permissions(SPEC.plan_id, f.operator, permissions=["read", "approve_cutover"],
        valid_after=f.now, expires_at=EXPIRES, expected_revision=0,
        administration_ref="fixture-force-reauth", command_key="reauth")
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    f.operator = f.identity.verifier.verify(f.identity.token(iat=EPOCH + 1, nbf=EPOCH + 1))
    with pytest.raises(EventError, match="forbidden"):
        f.approve()
    f.operator = f.identity.verifier.verify(f.identity.token(iat=EPOCH + 1, nbf=EPOCH + 1,
                                                            auth_time=EPOCH + 1))
    assert f.approve()["actor_id"] == f.operator.subject_id


def test_token_expiry_is_checked_after_waiting_for_plan_lock(secured):
    f = secured
    f.grant()
    approval = f.approve()
    started = Event()
    def submit():
        started.set()
        return f.submit(approval)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with f.backend.store._transaction(write=True, plan_id=SPEC.plan_id):
            future = pool.submit(submit)
            assert started.wait(5)
            f.now = "2026-11-03T10:05:00+00:00"
        with pytest.raises(EventError, match="forbidden"):
            future.result(timeout=10)
    assert not f.requests


def test_grant_is_scoped_to_one_plan(secured):
    f = secured
    f.grant()
    other = replace(SPEC, plan_id="plan-other")
    f.backend.store.register_plan(other, run_id="fixture-other", occurred_at=NOW)
    with f.backend.store._transaction() as c:
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (other.plan_id,)).fetchone()
        with pytest.raises(EventError, match="forbidden"):
            f.access.require(c, f.operator, dict(row), "read")
