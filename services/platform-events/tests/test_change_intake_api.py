"""Real HTTP handlers, signatures and DB; synthetic verification and identities."""

import json
import ssl
from dataclasses import asdict, replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_change_intake import BINDING, NORMAL, PLAN, SOURCE, IntakeFixture
from test_cutover import BINDINGS, EXPIRES, NOW
from test_operator_access import Identity

from bravo_platform_events.change_intake_api import PATH, HttpChangeIntake, IntakeDeliveryUnknown
from bravo_platform_events.operator_access import OperatorAccess, create_operator_cutover_app


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def f(backend, signing_key):
    return IntakeFixture(backend, signing_key)


def app_for(f, **kwargs):
    return create_operator_cutover_app(f.backend.store, object(), bindings=BINDINGS,
        verifier=kwargs.pop("verifier", lambda _request: None), clock=lambda: NOW,
        change_intake=f.intake, **kwargs)


def post(client, token, body=None):
    return client.post(PATH, json=asdict(NORMAL) if body is None else body,
                       headers={"Authorization": f"Bearer {token}"})


def test_receipt_is_not_human_approval_and_reopen_reuses_one_intake(f, signing_key):
    identity = Identity(signing_key)
    access = OperatorAccess(f.backend.store, clock=lambda: NOW)
    access.set_permissions(PLAN.plan_id, identity.principal(), permissions=["read", "approve_change"],
        valid_after=NOW, expires_at=EXPIRES, expected_revision=0,
        administration_ref="fixture-admin", command_key="grant")
    with TestClient(app_for(f, verifier=identity.verifier), base_url="https://t10.invalid") as client:
        first = post(client, f.token)
        assert first.status_code == 200
        assert set(first.json()) == {"verification_id", "attempt", "revision"}
        assert first.headers["cache-control"] == "no-store"
        approval = dict(approval_kind="change", run_id=NORMAL.run_id, binding=BINDING, expected_version=1)
        url = f"/v1/plans/{PLAN.plan_id}/approvals"
        assert client.post(url, json=approval, headers={"Authorization": f"Bearer {f.token}",
            "Idempotency-Key": "not-a-human"}).status_code == 403
        assert client.post(url, json=approval, headers={"Authorization": f"Bearer {identity.token()}",
            "Idempotency-Key": "human"}).status_code == 200
        assert post(client, identity.token()).status_code == 403
        for path in (PATH + "/hold", f"/v1/plans/{PLAN.plan_id}/apply"):
            assert client.post(path, json={}).status_code == 404
    f.intake.store = f.backend.reopen()
    f.backend.store = f.intake.store
    with TestClient(app_for(f), base_url="https://t10.invalid") as client:
        assert post(client, f.token).json() == first.json()
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?", ("change-intake-proof-%",)).fetchone()["n"] == 1


@pytest.mark.parametrize("fault", ["missing", "wrong", "duplicate", "http"])
def test_unauthenticated_request_does_not_read_original(f, fault):
    base = "http://t10.invalid" if fault == "http" else "https://t10.invalid"
    headers = [("Authorization", f"Bearer {f.token}")]
    if fault == "missing":
        headers = []
    elif fault == "wrong":
        headers = [("Authorization", "Bearer not-valid")]
    elif fault == "duplicate":
        headers *= 2
    with TestClient(app_for(f), base_url=base) as client:
        response = client.post(PATH, json=asdict(NORMAL), headers=headers)
    assert response.status_code == 403
    assert not f.reads
    assert f.token not in response.text


@pytest.mark.parametrize("extra", ["verdict", "original", "created_uid", "hold", "tenant_id"])
def test_sender_cannot_supply_original_or_management_fields(f, extra):
    with TestClient(app_for(f), base_url="https://t10.invalid") as client:
        response = post(client, f.token, asdict(NORMAL) | {extra: "PRIVATE_SENTINEL"})
    assert response.status_code == 422
    assert not f.reads
    assert "PRIVATE_SENTINEL" not in response.text


def test_unverified_mismatched_and_held_records_remain_unavailable(f):
    with TestClient(app_for(f), base_url="https://t10.invalid") as client:
        for original in (None, replace(SOURCE, removed_uid="different"),
                         replace(SOURCE, candidate=replace(NORMAL, image_digest="sha256:" + "e" * 64))):
            f.original = original
            assert post(client, f.token).status_code == 503
        f.original = SOURCE
        f.hold()
        assert post(client, f.token).json() == {"detail": {"code": "verification_held"}}


def test_input_and_storage_errors_do_not_expose_payload_or_credentials(f):
    with TestClient(app_for(f), base_url="https://t10.invalid") as client:
        for raw, status in ((b"{PRIVATE_SENTINEL", 422), (b"x" * 16385, 413)):
            response = client.post(PATH, content=raw, headers={"Content-Type": "application/json",
                "Authorization": f"Bearer {f.token}"})
            assert response.status_code == status
            assert "PRIVATE_SENTINEL" not in response.text
        with f.backend.reject_insert("plan_commands"):
            response = post(client, f.token)
        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "intake_unavailable"}}


def test_route_requires_explicit_intake_and_one_store_authority(f):
    no_intake = create_operator_cutover_app(f.backend.store, object(), bindings=BINDINGS,
                                           verifier=lambda _request: None)
    with TestClient(no_intake, base_url="https://t10.invalid") as client:
        assert post(client, f.token).status_code == 404
    with pytest.raises(ValueError, match="single_change_intake"):
        app_for(f, change_authority=f.intake.current)
    f.intake.store = f.backend.reopen()
    with pytest.raises(ValueError, match="single_change_intake"):
        app_for(f)


@pytest.mark.parametrize("origin", ["http://t10.invalid", "http://127.0.0.1", "https://user:secret@t10.invalid",
                                   "https://t10.invalid/path", "https://t10.invalid?token=secret"])
def test_client_rejects_unsafe_origin_before_any_request(origin):
    with pytest.raises(ValueError, match="invalid_intake_origin"):
        HttpChangeIntake(origin)


def test_client_requires_verified_tls_context():
    with pytest.raises(ValueError, match="verified_tls_context_required"):
        HttpChangeIntake("https://t10.invalid", tls_context=ssl._create_unverified_context())


@pytest.mark.parametrize("fault", ["redirect", "wrong-id", "wrong-attempt", "missing", "oversize", "timeout"])
def test_client_never_retries_and_redacts_failed_delivery(fault):
    calls = []
    def serve(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("PRIVATE_SENTINEL", request=request)
        receipt = dict(verification_id=NORMAL.verification_id, attempt=NORMAL.attempt, revision="c" * 64)
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid/"})
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * 8193)
        if fault == "wrong-id":
            receipt["verification_id"] = "another"
        if fault == "wrong-attempt":
            receipt["attempt"] = True
        if fault == "missing":
            receipt.pop("revision")
        return httpx.Response(200, json=receipt)
    client = HttpChangeIntake("https://t10.invalid", transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(IntakeDeliveryUnknown) as error:
            client.receive("fixture.token.signature", NORMAL)
        assert str(error.value) == "intake_delivery_unknown"
        assert len(calls) == 1
        assert json.loads(calls[0].content) == asdict(NORMAL)
    finally:
        client.close()


def test_lost_http_ack_leaves_one_record_and_explicit_redelivery_only(f):
    calls = []
    with TestClient(app_for(f), base_url="https://t10.invalid") as server:
        def serve(request):
            calls.append(request)
            result = server.post(PATH, content=request.content, headers=request.headers)
            if len(calls) == 1:
                assert result.status_code == 200
                raise httpx.ReadTimeout("ack lost after commit", request=request)
            return httpx.Response(result.status_code, json=result.json())
        client = HttpChangeIntake("https://t10.invalid", transport=httpx.MockTransport(serve))
        try:
            with pytest.raises(IntakeDeliveryUnknown):
                client.receive(f.token, NORMAL)
            assert len(calls) == 1
            receipt = client.receive(f.token, NORMAL)  # host-directed redelivery, no rehearsal submit
            assert receipt == f.deliver()
        finally:
            client.close()
    with f.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?", ("change-intake-proof-%",)).fetchone()["n"] == 1
