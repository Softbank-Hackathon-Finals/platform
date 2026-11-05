"""Synthetic SigV4 proofs; no AWS API or local SSO cache is used in these tests."""

import base64
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import boto3
import botocore.auth
import httpx
import pytest
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from fastapi.testclient import TestClient
from test_registration import SRC, Fixture, Probe

from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanStore
from bravo_platform_events.registration_api import create_registration_app
from bravo_platform_events.registration_probe import KubernetesProbe
from bravo_platform_events.registration_server import build_app
from bravo_platform_events.registration_sso import (
    AUDIENCE_HEADER,
    PREFIX,
    SsoRegistrationPrincipal,
    SsoRegistrationVerifier,
    make_registration_token,
    registration_subject,
)
from bravo_platform_events.store import EventError

NOW = datetime(2026, 11, 5, 11, 30, tzinfo=UTC)
ROLE = "AWSReservedSSO_Example_" + "a" * 16
CONFIG = dict(region="ap-northeast-2", audience="bravo-registration-test", expected_account="123456789012",
              expected_caller_arn="arn:aws:sts::123456789012:assumed-role/" + ROLE + "/example-human",
              expected_sso_role_arn="arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/" + ROLE,
              expected_sso_role_id="AROA" + "X" * 17, tenant_id="team")
IDENTITY = dict(Account=CONFIG["expected_account"], Arn=CONFIG["expected_caller_arn"],
                UserId=CONFIG["expected_sso_role_id"] + ":example-human")


@pytest.fixture
def proof(monkeypatch):
    clock = [NOW.timestamp()]
    # Deliberately synthetic values. No environment, shared credentials or cache.
    creds = Credentials("ASIA" + "X" * 16, "synthetic-signing-key", "synthetic-session", method="sso")
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: NOW)
    monkeypatch.setattr(boto3, "Session", lambda **_: SimpleNamespace(get_credentials=lambda: creds))
    token = make_registration_token(profile_name="test-explicit", region=CONFIG["region"], audience=CONFIG["audience"])
    return SimpleNamespace(clock=clock, creds=creds, token=token, calls=[])


def decoded(token):
    value = token[len(PREFIX):]
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def encoded(url):
    return PREFIX + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def change(token, **changes):
    url = urlsplit(decoded(token))
    query = dict(parse_qsl(url.query))
    for key, value in changes.items():
        if value is None:
            query.pop(key)
        else:
            query[key] = value
    return encoded(url._replace(query=urlencode(query)).geturl())


def verifier(proof, *, identity=None, reply=None):
    def sts(request):
        proof.calls.append(request)
        if reply:
            return reply(request)
        # Recompute the real SDK signature with the server-supplied audience.
        url = str(request.url)
        unsigned = urlsplit(url)._replace(query="Action=GetCallerIdentity&Version=2011-06-15").geturl()
        signed = AWSRequest(method="GET", url=unsigned, headers={AUDIENCE_HEADER: request.headers[AUDIENCE_HEADER]})
        SigV4QueryAuth(proof.creds, "sts", CONFIG["region"], expires=60).add_auth(signed)
        if dict(parse_qsl(urlsplit(url).query)) != dict(parse_qsl(urlsplit(signed.url).query)):
            return httpx.Response(403, text="private AWS diagnostic")
        return httpx.Response(200, json={"GetCallerIdentityResponse": {"GetCallerIdentityResult": identity or IDENTITY}})
    return SsoRegistrationVerifier(**CONFIG, clock=lambda: proof.clock[0], transport=httpx.MockTransport(sts))


def test_verified_exact_sso_is_registration_only(proof, caplog):
    caplog.set_level(logging.DEBUG)
    # The synthetic STS implementation also signs; real STS is external.
    caplog.set_level(logging.WARNING, logger="botocore.auth")
    actor = verifier(proof).verify(proof.token)
    assert isinstance(actor, SsoRegistrationPrincipal) and not isinstance(actor, OperatorPrincipal)
    assert not hasattr(actor, "authenticated_at")
    assert actor.subject_id == registration_subject(CONFIG["expected_account"], CONFIG["expected_sso_role_id"], CONFIG["expected_caller_arn"])
    assert actor.tenant_id == "team" and actor.expires_at == int(NOW.timestamp()) + 60
    assert proof.token not in repr(actor) and len(proof.calls) == 1
    assert decoded(proof.token) not in caplog.text and "synthetic-session" not in caplog.text


@pytest.mark.parametrize("logger_name", ["botocore", "botocore.auth", "botocore.endpoint", "botocore.parsers",
    "botocore.credentials", "botocore.tokens", "botocore.future_child", "boto3", "urllib3.connectionpool"])
def test_client_refuses_sdk_debug_before_reading_credentials(proof, monkeypatch, caplog, logger_name):
    caplog.set_level(logging.WARNING, logger="botocore")
    caplog.set_level(logging.WARNING, logger="botocore.auth")
    caplog.set_level(logging.DEBUG, logger=logger_name)
    monkeypatch.setattr(boto3, "Session", lambda **_: pytest.fail("Must not read credentials"))
    with pytest.raises(RuntimeError, match="^registration_sso_proof_unavailable$"):
        make_registration_token(profile_name="explicit", region=CONFIG["region"], audience=CONFIG["audience"])
    assert "synthetic-session" not in caplog.text


def test_wire_debug_is_rejected_before_credentials(proof, monkeypatch):
    from http.client import HTTPConnection
    monkeypatch.setattr(HTTPConnection, "debuglevel", 1)
    monkeypatch.setattr(boto3, "Session", lambda **_: pytest.fail("Must not read credentials"))
    with pytest.raises(RuntimeError, match="^registration_sso_proof_unavailable$"):
        make_registration_token(profile_name="explicit", region=CONFIG["region"], audience=CONFIG["audience"])


@pytest.mark.parametrize("permission", ["approve_change", "approve_cutover"])
def test_registration_proof_cannot_authorize_plan_approval(proof, permission):
    actor = verifier(proof).verify(proof.token)
    with pytest.raises(EventError, match="^forbidden$"):
        OperatorAccess(None).require(None, actor, {"tenant_id": "team"}, permission)


@pytest.mark.parametrize(("key", "value"), [
    ("Action", "AssumeRole"), ("Version", "2010-01-01"), ("X-Amz-Algorithm", "none"),
    ("X-Amz-Expires", "900"), ("X-Amz-Expires", "060"), ("X-Amz-Date", "20261105T113100Z"),
    ("X-Amz-Date", "20261105T112900Z"), ("X-Amz-Date", "garbage"),
    ("X-Amz-SignedHeaders", "host"), ("X-Amz-SignedHeaders", "host;authorization;" + AUDIENCE_HEADER),
    ("X-Amz-Security-Token", None), ("X-Amz-Security-Token", ""), ("Tenant", "other"),
    ("X-Amz-Credential", "ASIA" + "X" * 16 + "/20261105/us-east-1/sts/aws4_request"),
    ("X-Amz-Credential", "ASIA" + "X" * 16 + "/20261105/ap-northeast-2/s3/aws4_request"),
    ("X-Amz-Credential", "AKIA" + "X" * 16 + "/20261105/ap-northeast-2/sts/aws4_request"),
    ("X-Amz-Signature", "g" * 64),
])
def test_bad_proof_is_rejected_before_network(proof, key, value):
    assert verifier(proof).verify(change(proof.token, **{key: value})) is None
    assert proof.calls == []


@pytest.mark.parametrize("mutation", [
    lambda u: u.replace("https://", "http://"), lambda u: u.replace("sts.", "sts.attacker."),
    lambda u: u.replace("amazonaws.com/", "amazonaws.com:443/"), lambda u: u.replace("https://", "https://user@"),
    lambda u: u.replace("/?", "/other?"), lambda u: u + "#fragment", lambda u: u + "&Action=GetCallerIdentity",
    lambda u: u + "&action=GetCallerIdentity", lambda u: "\n" + u,
])
def test_no_endpoint_or_query_ambiguity(proof, mutation):
    assert verifier(proof).verify(encoded(mutation(decoded(proof.token)))) is None
    assert proof.calls == []


def test_wrong_signed_audience_or_modified_signature_is_denied(proof):
    other = make_registration_token(profile_name="test-explicit", region=CONFIG["region"], audience="wrong-service")
    assert verifier(proof).verify(other) is None
    assert verifier(proof).verify(change(proof.token, **{"X-Amz-Signature": "0" * 64})) is None
    assert len(proof.calls) == 2


@pytest.mark.parametrize(("field", "value"), [
    ("Account", "222222222222"), ("Arn", CONFIG["expected_caller_arn"].replace("example-human", "other-human")),
    ("UserId", "AROA" + "Y" * 17 + ":example-human"), ("UserId", CONFIG["expected_sso_role_id"] + ":other-human"),
])
def test_account_session_and_immutable_role_id_must_all_match(proof, field, value):
    assert verifier(proof, identity=IDENTITY | {field: value}).verify(proof.token) is None


def test_expiration_is_checked_again_after_sts(proof):
    def reply(_):
        proof.clock[0] += 60
        return httpx.Response(200, json={"GetCallerIdentityResponse": {"GetCallerIdentityResult": IDENTITY}})
    assert verifier(proof, reply=reply).verify(proof.token) is None


@pytest.mark.parametrize("status,body", [(302, "private redirect"), (500, "private error"), (200, "x" * 16385), (200, "bad json")])
def test_failure_is_bounded_and_diagnostics_are_suppressed(proof, status, body):
    with pytest.raises(RuntimeError, match="^registration_identity_unavailable$"):
        verifier(proof, reply=lambda _: httpx.Response(status, text=body, headers={"Location": "https://attacker.invalid"})).verify(proof.token)
    assert len(proof.calls) == 1


@pytest.mark.parametrize("method", ["env", "shared-credentials-file", "assume-role", "iam-role"])
def test_client_rejects_credential_fallbacks(proof, method):
    proof.creds.method = method
    with pytest.raises(RuntimeError, match="^registration_sso_proof_unavailable$"):
        make_registration_token(profile_name="explicit", region=CONFIG["region"], audience=CONFIG["audience"])


@pytest.mark.parametrize("fault", ["expired", "revoked"])
def test_registry_rechecks_after_probe_before_write(backend, tmp_path, proof, fault):
    fixture = Fixture(backend, tmp_path)
    identity = verifier(proof)
    fixture.registry.clock = lambda: datetime.fromtimestamp(proof.clock[0], UTC)
    def after():
        if fault == "expired":
            proof.clock[0] += 60
        else:
            fixture.allowed = False
    fixture.probe.after = after
    with TestClient(create_registration_app(fixture.registry, verify_identity=identity)) as client:
        r = client.post("/v1/environments", json=SRC, headers={"Authorization": "Bearer " + proof.token, "Idempotency-Key": "test"})
        assert r.status_code == 403
    with fixture.store._transaction() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM registrations").fetchone()["n"] == 0
    fixture.client.close()


def test_opt_in_entrypoint_current_grants_and_redacted_errors(tmp_path, monkeypatch, proof):
    import bravo_platform_events.registration_server as server
    identity = verifier(proof)
    actor = identity.verify(proof.token)
    monkeypatch.setattr(server, "SsoRegistrationVerifier", lambda **_: identity)
    monkeypatch.setattr(KubernetesProbe, "inspect", lambda _, env: Probe().inspect(env))
    config = dict(identity_mode="aws_sso", sso_verifier=CONFIG, environments=[dict(tenant_id="team",
        environment_id="src", connection_ref="source-reader", kind="aws", namespace="src", database="db",
        database_uid="database", endpoint="https://api.example.invalid", ca_file=str(tmp_path / "ca"), token_file=str(tmp_path / "token"))], releases=[])
    catalog, grants = tmp_path / "catalog.json", tmp_path / "grants.json"
    catalog.write_text(json.dumps(config))
    grants.write_text(json.dumps([dict(tenant_id="team", subject_id=actor.subject_id)]))
    # Registry uses wall clock; use a fresh signed timestamp for this entrypoint.
    current = datetime.now(UTC)
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: current)
    proof.clock[0] = current.timestamp()
    token = make_registration_token(profile_name="explicit", region=CONFIG["region"], audience=CONFIG["audience"])
    with TestClient(build_app(PlanStore(tmp_path / "api.db"), catalog, grants)) as client:
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "test"}
        assert client.post("/v1/environments", json=SRC, headers=headers).status_code == 201
        assert client.post("/v1/environments", json=SRC, headers=headers).status_code == 200
        assert client.post("/v1/environments", json=SRC, headers=list(headers.items()) + [("Authorization", "Bearer another")]).status_code == 403
        grants.write_text("[]")
        assert client.post("/v1/environments", json=SRC, headers=headers).status_code == 403
        bad = client.post("/v1/environments", json=SRC | {"token": token}, headers=headers)
        assert bad.status_code == 422 and token not in bad.text
    catalog.write_text(json.dumps(config | {"operator_verifier": {}}))
    with pytest.raises(ValueError, match="invalid_registration_identity_mode"):
        build_app(PlanStore(tmp_path / "other.db"), catalog, grants)
