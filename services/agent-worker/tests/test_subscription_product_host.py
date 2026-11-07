import asyncio
import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tls_support import TEST_CA_PEM

from bravo_agent.contracts import digest
from bravo_agent.coordinator import Coordinator
from bravo_agent.product_contracts import validate_product_input
from bravo_agent.subscription_product import ProductInput, proposal_result
from bravo_agent.subscription_product_host import ProductTunnelCall, RegisteredProductInputs

NOW = datetime(2026, 11, 5, tzinfo=UTC)
PRINCIPAL = "b" * 64
RUNTIME = "c" * 40


@pytest.fixture
def registration(context):
    bundle, snapshot, _, _ = context
    admission = {"plan_id": "plan-1", "tenant_id": "tenant-1", "plan_version": 1,
                 "task_id": "task-1", "rehearsal_id": bundle.run_id, "attempt": 1,
                 "source_sha": bundle.source_sha, "task_digest": "d" * 64}
    value = ProductInput(admission_digest=digest(admission), attempt=1, bundle=bundle,
                         manifest=snapshot.manifest,
                         files={p: snapshot.read(p) for p in snapshot.manifest.files})
    return {"admission": admission, "product_input": value.model_dump(), "input_digest": digest(value),
            "principal": PRINCIPAL, "purpose": "t38-product-proposal",
            "expires_at": (NOW + timedelta(minutes=10)).isoformat()}


def test_registered_input_is_detached_and_reader_gets_only_identity_and_reference(registration):
    calls = []

    def read(principal, ref):
        calls.append((principal, ref))
        return registration

    value = RegisteredProductInputs(read, lambda: NOW).resolve(PRINCIPAL, "input-1")
    assert digest(value) == registration["input_digest"]
    assert value.admission_digest == digest(registration["admission"])
    assert digest(value.bundle) != registration["admission"]["task_digest"]
    registration["product_input"]["files"]["src/config.py"] = "changed"
    assert value.files["src/config.py"] != "changed"
    assert calls == [(PRINCIPAL, "input-1")]


@pytest.mark.parametrize("field,value", [
    ("principal", "f" * 64), ("purpose", "other-purpose"), ("input_digest", "f" * 64),
    ("expires_at", NOW.isoformat()), ("expires_at", "2026-11-06T00:00:00"),
    ("expires_at", "2026-11-06T00:00:00+09:00"), ("extra", True),
])
def test_registration_fault_rejected(registration, field, value):
    registration[field] = value
    with pytest.raises(ValueError, match="registered product input unavailable"):
        RegisteredProductInputs(lambda *_: registration, lambda: NOW).resolve(PRINCIPAL, "input-1")


@pytest.mark.parametrize("field,value", [
    ("plan_id", "other-plan"), ("tenant_id", "other-tenant"), ("plan_version", 2),
    ("task_id", "other-task"), ("rehearsal_id", "other-run"), ("source_sha", "f" * 40),
    ("task_digest", "f" * 64), ("attempt", 2), ("attempt", True),
])
def test_every_admission_field_is_bound(registration, field, value):
    registration["admission"][field] = value
    with pytest.raises(ValueError):
        RegisteredProductInputs(lambda *_: registration, lambda: NOW).resolve(PRINCIPAL, "input-1")


def test_slow_read_cannot_extend_expiry(registration):
    clock = [NOW]

    def read(*_):
        clock[0] += timedelta(hours=1)
        return registration

    with pytest.raises(ValueError):
        RegisteredProductInputs(read, lambda: clock[0]).resolve(PRINCIPAL, "input-1")


def test_revoked_record_is_reread_and_reader_error_is_not_disclosed(registration):
    calls = []

    def read(*args):
        calls.append(args)
        if len(calls) > 1:
            raise ValueError("private-source-or-token-value")
        return registration

    inputs = RegisteredProductInputs(read, lambda: NOW)
    inputs.resolve(PRINCIPAL, "input-1")
    with pytest.raises(ValueError) as error:
        inputs.resolve(PRINCIPAL, "input-1")
    assert str(error.value) == "registered product input unavailable"
    assert len(calls) == 2


@pytest.mark.parametrize("principal,ref", [("bad", "input-1"), (PRINCIPAL, "../input")])
def test_invalid_lookup_does_not_read(principal, ref):
    calls = []
    with pytest.raises(ValueError):
        RegisteredProductInputs(lambda *args: calls.append(args)).resolve(principal, ref)
    assert calls == []


@pytest.fixture
def exchange(context, registration):
    bundle, snapshot, proposal, _ = context
    value = ProductInput.model_validate(registration["product_input"])

    class Provider:
        async def propose(self, bundle, tools):
            tools.dispatch("mcp__bravo__plan_propose", proposal)

    raw = asyncio.run(Coordinator(bundle, snapshot).propose(Provider()))
    result = proposal_result(raw, value, snapshot, {
        "product_input_digest": digest(value), "admission_digest": value.admission_digest,
        "runtime_source_sha": RUNTIME,
    })
    body = {"input_ref": "input-1", "input_digest": digest(value), "web_identity_token": "synthetic-oidc"}
    return body, result


def test_fixed_tunnel_post_preserves_product_response(exchange):
    body, result = exchange
    calls = []

    def serve(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://127.0.0.1:18762/run"
        assert json.loads(request.content) == body
        return httpx.Response(200, json=result)

    call = ProductTunnelCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(serve))
    assert call(body) == (200, result)
    assert len(calls) == 1
    assert result["input_digest"] != body["input_digest"]
    assert result["product_input_digest"] == body["input_digest"]
    assert result["tool_events_status"] == "UNAVAILABLE"


@pytest.mark.parametrize("field,value", [
    ("runtime_source_sha", "e" * 40), ("runtime_source_sha", None),
    ("task_digest", "e" * 64), ("task", {}), ("product_input_digest", "e" * 64),
    ("attempt", True), ("attempt", 2), ("state", "AWAITING_APPROVAL"),
    ("tool_events_status", "COMPLETE"), ("tool_events", []),
    ("input_kind", "synthetic_db_host_fixture"), ("admission_digest", "invalid"),
])
def test_bad_projection_never_reaches_consumer(exchange, field, value):
    body, result = exchange
    result[field] = value
    call = ProductTunnelCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(lambda _: httpx.Response(200, json=result)))
    assert call(body) == (502, {"state": "NEEDS_HUMAN", "reason": "PRODUCT_CALL_UNCONFIRMED"})


@pytest.mark.parametrize("failure", ["redirect", "lost_response", "private_error", "oversized", "invalid_json"])
def test_transport_failure_never_retries_redirects_or_discloses(exchange, failure):
    body, _ = exchange
    calls = []

    def serve(request):
        calls.append(request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://other.invalid/run"})
        if failure == "lost_response":
            raise httpx.ReadTimeout("private-token-value")
        if failure == "private_error":
            return httpx.Response(500, text="private-token-value")
        if failure == "oversized":
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        return httpx.Response(200, text="private-token-value")

    status, result = ProductTunnelCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(serve))(body)
    assert status != 200 and len(calls) == 1
    assert result == {"state": "NEEDS_HUMAN", "reason": "PRODUCT_CALL_UNCONFIRMED"}


def test_invalid_body_does_not_contact_tunnel(exchange):
    body, _ = exchange
    body = copy.deepcopy(body)
    body["source_files"] = {}
    calls = []
    status, _ = ProductTunnelCall(RUNTIME, TEST_CA_PEM, httpx.MockTransport(lambda req: calls.append(req)))(body)
    assert status == 502 and calls == []


def test_contract_import_has_no_model_or_network_runtime():
    script = """
import sys
from bravo_agent.product_contracts import ProductInput, ProductRequest, validate_product_input
blocked = ('openai', 'boto3', 'httpx', 'bravo_agent.subscription_admin',
           'bravo_agent.subscription_product', 'bravo_agent.coordinator')
assert not any(name in sys.modules for name in blocked)
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fault", ["hash", "extra_file", "source", "secret", "byte_limit"])
def test_pure_source_check_rejects_without_staging(registration, fault):
    import hashlib

    raw = registration["product_input"]
    if fault == "hash":
        raw["files"]["src/config.py"] = "changed"
    elif fault == "extra_file":
        raw["files"]["src/extra.py"] = "extra"
    elif fault == "source":
        raw["manifest"]["source_sha"] = "f" * 40
    else:
        text = 'DB_PASSWORD = "private-value-1234"' if fault == "secret" else "가" * 11000
        raw["files"]["src/config.py"] = text
        raw["manifest"]["files"]["src/config.py"] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(ValueError):
        validate_product_input(raw)


def test_the_infra_plans_the_model_asked_for_reach_the_response_unchanged(context, registration):
    bundle, snapshot, proposal, _ = context
    value = ProductInput.model_validate(registration["product_input"])

    class Provider:
        async def propose(self, bundle, tools):
            tools.dispatch("mcp__bravo__plan_propose", proposal)

    raw = asyncio.run(Coordinator(bundle, snapshot).propose(Provider()))
    expected = {"product_input_digest": digest(value), "admission_digest": value.admission_digest,
                "runtime_source_sha": RUNTIME}
    assert "infra_plans" not in proposal_result(raw, value, snapshot, expected)  # none asked: field absent
    plan = {"kind": "iac-plan.v1", "plan_digest": "sha256:" + "a" * 64, "accepted": True, "refusals": []}
    raw["attempts"][-1]["infra_plans"] = [{"module": "target-object-exposure", "params": {"state": "closed"},
                                           "plan": plan}]
    result = proposal_result(raw, value, snapshot, expected)
    assert result["infra_plans"] == [plan]
    from bravo_agent.proposal_contracts import validate_bound_product_response

    assert validate_bound_product_response(result, digest(value), RUNTIME) == result
    raw["attempts"][-1]["infra_plans"] = [{"plan": plan}]  # not the tool's shape
    with pytest.raises(ValueError):
        proposal_result(raw, value, snapshot, expected)
