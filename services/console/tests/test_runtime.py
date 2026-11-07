import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_platform_events.operator_access import OperatorAccess, create_operator_cutover_app
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanSpec, PlanStore
from fastapi.testclient import TestClient

from bravo_console.runtime import NativeCustomerSource, assemble


@pytest.fixture
def runtime(tmp_path):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "fixture_customer_reader", root / "infra/iac/customer_status.py"
    )
    customer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(customer)
    reads = []

    def get(kind, name=None, **_kw):
        reads.append((kind, name))
        if kind == "namespace":
            return {"metadata": {"uid": "fixture-customer"}}
        return {}  # actual collector interprets the absent Argo CRD; no success fixture

    registered = [
        dict(
            environment_id="env-target",
            environment="target-dev",
            cluster_uid="fixture-customer",
            reviewed_revision="b" * 40,
            kubeconfig="/registered/fixture.kubeconfig",
        )
    ]
    source = NativeCustomerSource(
        registered,
        inspect_customer=customer.inspect,
        reader=lambda path: get,
        producer_revision="c" * 40,
    )
    source.collect(source.targets[0])
    store = PlanStore(tmp_path / "runtime.db")
    now = datetime.now(UTC)
    store.register_plan(
        PlanSpec(
            "plan-real", "tenant-real", "chat", "env-source", "env-target", "a" * 64, "b" * 64
        ),
        run_id="register",
        occurred_at=now.isoformat(),
    )
    actor = OperatorPrincipal(
        "operator-real",
        "tenant-real",
        int(now.timestamp()),
        int((now + timedelta(hours=1)).timestamp()),
        int(now.timestamp()),
        "fixture-session",
    )
    access = OperatorAccess(store)
    access.set_permissions(
        "plan-real",
        actor,
        permissions=["read", "gate:read"],
        valid_after=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        expected_revision=0,
        administration_ref="fixture-admin",
        command_key="grant-read",
    )
    state = {"valid": True, "actor": actor, "access": access}

    def verifier(request):
        return (
            actor
            if state["valid"] and request.headers.get("authorization") == "Bearer fixture-proof"
            else None
        )

    api = create_operator_cutover_app(
        store,
        object(),
        bindings={"env-source": "registered/source", "env-target": "registered/target"},
        verifier=verifier,
    )
    host = SimpleNamespace(app=api, access=access)
    config = dict(
        version=1, public_origin="https://console.example.invalid", modules=[], customers=registered
    )
    app = assemble(host, store, verifier, config, source)
    return app, source, store, state, reads


def test_original_operator_auth_and_actual_customer_collector_are_composed(runtime):
    app, source, store, state, reads = runtime
    with TestClient(
        app, base_url=app.origin, headers={"Authorization": "Bearer fixture-proof"}
    ) as c:
        r = c.get("/v1/plans/plan-real/delivery")
        assert r.status_code == 200, r.text
        assert r.json()["customers"][0]["status"] == "NOT_INSTALLED"
        assert r.json()["customers"][0]["provenance"] == "live"
        assert r.json()["authorizes_execution"] is False
        assert r.json()["iac"]["configured"] is False
        assert not any(r.json()["iac"]["actions"].values())
        assert c.get("/console/").status_code == 200
        assert c.get("/v1/plans/foreign/delivery").status_code == 403
        state["valid"] = False
        assert c.get("/v1/plans/plan-real/delivery").status_code == 403
    assert reads[0] == reads[-1] == ("namespace", "kube-system")
    assert store.snapshot("plan-real")["state"] == "COLLECT"


def test_matching_hash_alone_never_authenticates_an_arbitrary_registration(runtime):
    _, source, store, _, _ = runtime
    plan = store.snapshot("plan-real")
    target = source.targets[0]
    item = source.load({}, plan, target)
    assert source.authenticate({}, plan, target, item)
    assert source.load({}, plan, target) is item
    assert not source.authenticate({}, plan, target, replace(item))
    source.documents.clear()
    assert not source.authenticate({}, plan, target, item)


def test_missing_operator_proof_returns_no_customer_data(runtime):
    app, *_ = runtime
    with TestClient(app, base_url=app.origin) as c:
        r = c.get("/v1/plans/plan-real/delivery")
        assert r.status_code == 403 and "fixture-customer" not in r.text


def test_revoking_native_plan_grant_removes_delivery_data(runtime):
    app, source, store, state, _ = runtime
    with TestClient(
        app, base_url=app.origin, headers={"Authorization": "Bearer fixture-proof"}
    ) as client:
        assert client.get("/v1/plans/plan-real/delivery").status_code == 200
        now = datetime.now(UTC)
        state["access"].set_permissions(
            "plan-real",
            state["actor"],
            permissions=[],
            valid_after=(now - timedelta(seconds=1)).isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            expected_revision=1,
            administration_ref="fixture-revoke-admin",
            command_key="revoke-read",
        )
        response = client.get("/v1/plans/plan-real/delivery")
        assert response.status_code == 403
        assert "fixture-customer" not in response.text


def test_customer_failures_clear_observation_and_issued_authority(runtime, monkeypatch):
    import asyncio

    _, source, store, _, _ = runtime
    target = source.targets[0]
    plan = store.snapshot("plan-real")
    issued = source.load({}, plan, target)

    def failure(*_args):
        raise RuntimeError("private-transport-error")

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(source, "reader", failure)
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(source.poll())
    assert not source.authenticate({}, plan, target, issued)
    with pytest.raises(ValueError, match="customer_collection_pending"):
        source.load({}, plan, target)


def test_image_bound_customer_producer_refuses_tampered_bytes(tmp_path, monkeypatch):
    import hashlib
    import json

    from bravo_console import server

    producer = tmp_path / "customer_status.py"
    producer.write_text("raise RuntimeError('must not execute tampered producer')")
    metadata = tmp_path / "runtime-source.json"
    metadata.write_text(json.dumps(dict(source_revision="a" * 40, customer_status_sha256="b" * 64)))
    monkeypatch.setattr(server, "RUNTIME_SOURCE", metadata)
    monkeypatch.setattr(server, "CUSTOMER_PRODUCER", producer)
    with pytest.raises(ValueError, match="customer_producer_source_mismatch"):
        server.customer_producer()
    producer.write_text("def inspect(): pass\ndef reader(): pass\n")
    metadata.write_text(
        json.dumps(
            dict(
                source_revision="a" * 40,
                customer_status_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
            )
        )
    )
    module, revision = server.customer_producer()
    assert callable(module.inspect) and callable(module.reader) and revision == "a" * 40
