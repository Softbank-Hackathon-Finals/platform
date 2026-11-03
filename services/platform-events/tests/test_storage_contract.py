"""The same observable plan/event/API contract against both real databases."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError
from test_plans import PROOF, SPEC, TIME, complete, progress

from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.store import EventError


def register(store, spec=SPEC):
    store.register_plan(spec, run_id="run-1", occurred_at=TIME)


def test_registration_reopen_transition_and_original_receipt(backend):
    store = backend.store
    register(store)
    assert store.snapshot(SPEC.plan_id)["last_event_id"] == "plan-demo:1"
    receipt = complete(store)
    reopened = backend.reopen()
    register(reopened)
    assert complete(reopened) == receipt
    assert receipt == dict(plan_id=SPEC.plan_id, state="ANALYZE", version=2, event_id="plan-demo:2")
    assert reopened.snapshot(SPEC.plan_id)["last_event_id"] == receipt["event_id"]
    assert len(reopened.read_after(SPEC.plan_id, None)) == 2
    assert reopened.tenant_for(SPEC.plan_id) == SPEC.tenant_id
    assert "tenant_id" not in reopened.snapshot(SPEC.plan_id)


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "workload_id",
        "source_environment_id",
        "target_environment_id",
        "plan_digest",
        "collection_scope_digest",
    ],
)
def test_registration_is_immutable(backend, field):
    register(backend.store)
    value = "c" * 64 if field.endswith("digest") else "other"
    with pytest.raises(EventError, match="plan_conflict"):
        register(backend.store, replace(SPEC, **{field: value}))
    assert backend.store.tenant_for(SPEC.plan_id) == SPEC.tenant_id
    assert len(backend.store.read_after(SPEC.plan_id, None)) == 1


def test_registration_and_stream_race_cannot_adopt_unmanaged_stream(backend):
    backend.store.register_stream(SPEC.plan_id)
    with pytest.raises(EventError, match="stream_conflict"):
        register(backend.store)
    assert backend.store.tenant_for(SPEC.plan_id) is None


def test_concurrent_registration_is_idempotent_across_instances(backend):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: register(backend.reopen()), range(16)))
    assert len(backend.store.read_after(SPEC.plan_id, None)) == 1


def test_competing_commands_and_retries_across_instances(backend):
    register(backend.store)
    barrier = Barrier(8)

    def compete(i):
        instance = backend.reopen()
        barrier.wait(timeout=10)
        try:
            return complete(instance, command_key=f"command-{i}")
        except EventError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 7
    assert len(backend.store.read_after(SPEC.plan_id, None)) == 2
    other = replace(SPEC, plan_id="retries")
    register(backend.store, other)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(
                lambda _: complete(backend.reopen(), proof=replace(PROOF, plan_id=other.plan_id)),
                range(16),
            )
        )
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(backend.store.read_after(other.plan_id, None)) == 2


def test_append_concurrency_order_dedup_and_key_conflict(backend):
    register(backend.store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda i: progress(backend.reopen(), f"exec-{i}"), range(16)))
    assert sorted(e["sequence"] for e in events) == list(range(2, 18))
    original = progress(backend.store, "retry")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(
            e == original
            for e in pool.map(lambda _: progress(backend.reopen(), "retry"), range(16))
        )
    with pytest.raises(EventError, match="producer_key_conflict"):
        backend.store.append(
            plan_id=SPEC.plan_id,
            run_id="other-run",
            producer_key="retry",
            event_type="execution.updated",
            occurred_at=TIME,
            data=original["data"],
        )
    assert [e["sequence"] for e in backend.store.read_after(SPEC.plan_id, None)] == list(
        range(1, 19)
    )


@pytest.mark.parametrize("table", ["events", "plan_commands"])
def test_failed_insert_rolls_back_state_event_counter_and_receipt(backend, table):
    register(backend.store)
    before = backend.store.snapshot(SPEC.plan_id)
    with backend.reject_insert(table), pytest.raises(backend.storage_error):
        complete(backend.store)
    assert backend.store.snapshot(SPEC.plan_id) == before
    assert len(backend.store.read_after(SPEC.plan_id, None)) == 1
    with backend.admin() as c:
        assert not c.execute("SELECT * FROM plan_commands").fetchall()
    assert complete(backend.store)["event_id"] == "plan-demo:2"


def test_schema_validation_failure_rolls_back_registration_and_transition(backend):
    with pytest.raises(ValidationError):
        backend.store.register_plan(SPEC, run_id="run-1", occurred_at="invalid")
    assert backend.store.tenant_for(SPEC.plan_id) is None
    register(backend.store)
    with pytest.raises(ValidationError):
        complete(backend.store, occurred_at="invalid")
    assert backend.store.snapshot(SPEC.plan_id)["version"] == 1
    assert complete(backend.store)["event_id"] == "plan-demo:2"


@pytest.mark.parametrize(
    "change,error",
    [
        ({"proof": replace(PROOF, complete=False)}, "collection_incomplete"),
        ({"proof": replace(PROOF, plan_digest="c" * 64)}, "collection_context_mismatch"),
        (
            {"proof": replace(PROOF, collection_scope_digest="c" * 64)},
            "collection_context_mismatch",
        ),
        ({"expected_version": 2}, "version_conflict"),
    ],
)
def test_transition_guards_do_not_write(backend, change, error):
    register(backend.store)
    with pytest.raises(EventError, match=error):
        complete(backend.store, **change)
    assert backend.store.snapshot(SPEC.plan_id)["last_event_id"] == "plan-demo:1"


def test_command_binding_and_managed_state_boundary(backend):
    register(backend.store)
    complete(backend.store)
    with pytest.raises(EventError, match="command_key_conflict"):
        complete(backend.store, run_id="other")
    with pytest.raises(EventError, match="transition_not_allowed"):
        complete(backend.store, command_key="other", expected_version=2)
    with pytest.raises(EventError, match="managed_state_required"):
        backend.reopen().append(
            plan_id=SPEC.plan_id,
            run_id="run-1",
            producer_key="unsafe",
            event_type="state.changed",
            occurred_at=TIME,
            data={"from": "ANALYZE", "to": "DONE"},
        )
    assert backend.store.snapshot(SPEC.plan_id)["state"] == "ANALYZE"


def test_snapshot_remains_consistent_while_other_instance_commits(backend, monkeypatch):
    register(backend.store)
    other = backend.reopen()
    original = backend.store._snapshot

    def overlap(c, plan):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(complete, other).result(timeout=10)
        return original(c, plan)

    monkeypatch.setattr(backend.store, "_snapshot", overlap)
    snapshot = backend.store.snapshot(SPEC.plan_id)
    assert (snapshot["state"], snapshot["version"], snapshot["last_event_id"]) == (
        "COLLECT",
        1,
        "plan-demo:1",
    )
    assert (
        backend.store.read_after(SPEC.plan_id, snapshot["last_event_id"])[0]["data"]["to"]
        == "ANALYZE"
    )


def test_tenant_auth_and_snapshot_then_sse(backend):
    register(backend.store)
    active = True
    client = TestClient(
        create_plan_app(
            backend.store,
            resolve_principal=lambda _: (
                Principal("fixture-user", SPEC.tenant_id) if active else None
            ),
        )
    )
    response = client.get(f"/v1/plans/{SPEC.plan_id}")
    assert response.status_code == 200
    complete(backend.store)
    resumed = client.get(
        f"/v1/plans/{SPEC.plan_id}/events?follow=false",
        headers={"Last-Event-ID": response.json()["last_event_id"]},
    )
    events = [
        json.loads(line[6:]) for line in resumed.text.splitlines() if line.startswith("data: ")
    ]
    assert [e["event_id"] for e in events] == ["plan-demo:2"]
    for principal in (None, Principal("fixture-user", "other-tenant")):
        denied = TestClient(
            create_plan_app(backend.store, resolve_principal=lambda _, p=principal: p)
        )
        for path in (SPEC.plan_id, f"{SPEC.plan_id}/events?follow=false", "unknown"):
            assert (
                denied.get(
                    f"/v1/plans/{path}",
                    headers={"X-Tenant-ID": SPEC.tenant_id, "Last-Event-ID": "bad"},
                ).status_code
                == 403
            )
    active = False
    assert client.get(f"/v1/plans/{SPEC.plan_id}").status_code == 403


@pytest.mark.parametrize(
    "cursor,error",
    [("other:1", "invalid_cursor"), ("bad", "invalid_cursor"), ("plan-demo:9", "resync_required")],
)
def test_cursor_plan_and_sequence_boundaries(backend, cursor, error):
    register(backend.store)
    with pytest.raises(EventError, match=error):
        backend.store.read_after(SPEC.plan_id, cursor)


def test_retention_gap_requires_resync_even_with_snapshot_watermark(backend):
    register(backend.store)
    complete(backend.store)
    progress(backend.store)
    with backend.admin() as c:
        c.execute("DELETE FROM events WHERE sequence = 2")
    assert backend.store.snapshot(SPEC.plan_id)["last_event_id"] == "plan-demo:3"
    with pytest.raises(EventError, match="resync_required"):
        backend.store.read_after(SPEC.plan_id, "plan-demo:1")
    with backend.admin() as c:
        c.execute("DELETE FROM events")
    with pytest.raises(EventError, match="resync_required"):
        backend.store.read_after(SPEC.plan_id, "plan-demo:3")
