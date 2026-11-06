"""Real RSA identity and SQLite/PostgreSQL claim/grant reads; no model dispatch."""

import time
from types import SimpleNamespace

import jwt
import pytest
import test_initial_proposal as fixture_module
from bravo_agent.product_execution_contracts import ExecutionPending
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi.testclient import TestClient
from test_cutover import SPEC

from bravo_platform_events.initial_proposal_api import (
    PATH,
    AuthenticatedInitialReader,
    InitialRead,
    create_initial_read_app,
)
from bravo_platform_events.initial_proposal_contracts import INITIAL_WORKFLOW
from bravo_platform_events.product_api import AUDIENCE, ISSUER, SUBJECT, GitHubProductCaller
from bravo_platform_events.workload_identity import WorkloadSender


@pytest.fixture
def api(backend, tmp_path, monkeypatch):
    key = generate_private_key(public_exponent=65537, key_size=2048)
    caller = GitHubProductCaller(WorkloadSender(ISSUER, AUDIENCE, SUBJECT, key.public_key(), SPEC.tenant_id),
                                "fixture-observed-provider")
    monkeypatch.setattr(fixture_module, "ACTOR", caller.principal)
    f = fixture_module.Fixture(backend, tmp_path)
    intents = []

    def submit(intent):
        intents.append(intent)
        raise ExecutionPending()

    f.client = SimpleNamespace(recipient=lambda _: "d" * 64, submit=submit,
                              read=lambda _: pytest.fail("unexpected read"), validate=lambda *_: None)
    f.port = f.execution()
    with pytest.raises(ExecutionPending):
        f.execute()
    intent = intents[0]
    reader = AuthenticatedInitialReader(f.records, caller)
    request = InitialRead(principal=caller.principal, intent=intent, github_run_id="1234")
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=AUDIENCE, sub=SUBJECT, iat=now, exp=now+300,
        repository_id="1443045223", repository_owner_id="350259663", run_id="1234", run_attempt="1",
        workflow_ref=f"Softbank-Hackathon-Finals/platform/.github/workflows/{INITIAL_WORKFLOW}@refs/heads/develop",
        workflow_sha=intent.runtime_source_sha, sha=intent.runtime_source_sha,
        event_name="workflow_dispatch", ref="refs/heads/develop")
    return f, reader, request, key, claims


def post(api, *, claim_change=None, body_change=None, scheme="https"):
    _, reader, request, key, claims = api
    token = jwt.encode(claims | (claim_change or {}), key, algorithm="RS256")
    with TestClient(create_initial_read_app(reader), base_url=scheme+"://reader.invalid") as client:
        return client.post(PATH, json=request.model_dump() | (body_change or {}),
                           headers={"Authorization": "Bearer " + token})


def test_exact_claim_v2_current_grant_and_no_rehearsal_route(api):
    response = post(api)
    assert response.status_code == 200 and response.json()["schema_version"] == 2
    assert response.json()["binding"]["stage"] == "PROPOSE"
    assert "admission_digest" not in response.json()
    assert response.headers["cache-control"] == "no-store"
    f, reader, *_ = api
    f.grant(False, 1)
    assert post(api).status_code == 409
    with TestClient(create_initial_read_app(reader), base_url="https://reader.invalid") as client:
        assert client.post("/internal/v1/product-inputs/read", json={}).status_code == 404


@pytest.mark.parametrize("change", [{"run_id": "9999"}, {"run_attempt": "2"},
    {"workflow_sha": "a" * 40}, {"sha": "a" * 40}, {"workflow_ref": "subscription-product.yml"},
    {"repository_id": "1"}, {"exp": 1}, {"event_name": "pull_request"}])
def test_other_identity_rejected_before_input_read(api, change, monkeypatch):
    monkeypatch.setattr(api[1].records, "_read", lambda *a, **k: pytest.fail("unauthenticated read"))
    assert post(api, claim_change=change).status_code == 403


def test_modified_claim_or_legacy_or_plain_http_is_rejected(api):
    request = api[2].model_dump()
    request["intent"]["recipient_public_key"] = "e" * 64
    assert post(api, body_change=request).status_code == 409
    assert post(api, body_change={"admission_digest": "a" * 64}).status_code == 422
    assert post(api, scheme="http").status_code == 403


def test_duplicate_fields_and_oversized_body_do_not_read(api, monkeypatch):
    _, reader, _, key, claims = api
    monkeypatch.setattr(reader.records, "_read", lambda *a, **k: pytest.fail("invalid read"))
    headers = {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256"),
               "Content-Type": "application/json"}
    with TestClient(create_initial_read_app(reader), base_url="https://reader.invalid") as client:
        assert client.post(PATH, content='{"principal":"a","principal":"b"}', headers=headers).status_code == 422
        assert client.post(PATH, content="x" * 16385, headers=headers).status_code == 413
