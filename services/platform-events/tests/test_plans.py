import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import CollectionProof, PlanSpec, PlanStore
from bravo_platform_events.store import EventError, EventStore

SPEC = PlanSpec(
    "plan-demo", "tenant-demo", "workload-demo", "env-source", "env-target", "a" * 64, "b" * 64
)
PROOF = CollectionProof(
    SPEC.plan_id, SPEC.plan_digest, SPEC.collection_scope_digest, "evidence-collection", True
)
TIME = "2026-11-03T10:00:00Z"


@pytest.fixture
def store(tmp_path):
    result = PlanStore(tmp_path / "plans.db")
    result.register_plan(SPEC, run_id="run-1", occurred_at=TIME)
    return result


def complete(store, **kwargs):
    args = dict(
        proof=PROOF, expected_version=1, command_key="collect-1", run_id="run-1", occurred_at=TIME
    )
    args.update(kwargs)
    return store.complete_collection(**args)


def progress(store, key="exec-1"):
    return store.append(
        plan_id=SPEC.plan_id,
        run_id="run-1",
        producer_key=key,
        event_type="execution.updated",
        occurred_at=TIME,
        data={"execution_id": "fixture-1", "step": "collect", "status": "succeeded"},
    )


def test_registration_transition_restart_and_original_receipt(store):
    initial = store.snapshot(SPEC.plan_id)
    assert (initial["state"], initial["version"], initial["last_event_id"]) == (
        "COLLECT",
        1,
        "plan-demo:1",
    )
    assert "tenant_id" not in initial
    receipt = complete(store)
    reopened = PlanStore(store.path)
    assert complete(reopened) == receipt
    reopened.register_plan(SPEC, run_id="run-other", occurred_at=TIME)
    events = reopened.read_after(SPEC.plan_id, None)
    assert [e["data"] for e in events] == [
        {"from": None, "to": "COLLECT"},
        {"from": "COLLECT", "to": "ANALYZE"},
    ]
    assert receipt == dict(plan_id=SPEC.plan_id, state="ANALYZE", version=2, event_id="plan-demo:2")
    assert reopened.snapshot(SPEC.plan_id)["last_event_id"] == receipt["event_id"]


@pytest.mark.parametrize(
    "change",
    [
        dict(tenant_id="other"),
        dict(workload_id="other"),
        dict(plan_digest="c" * 64),
        dict(collection_scope_digest="c" * 64),
        dict(source_environment_id="other"),
        dict(target_environment_id="other"),
    ],
)
def test_registration_cannot_rebind_existing_plan(store, change):
    before = store.snapshot(SPEC.plan_id)
    with pytest.raises(EventError, match="plan_conflict"):
        store.register_plan(replace(SPEC, **change), run_id="run-1", occurred_at=TIME)
    assert store.snapshot(SPEC.plan_id) == before
    assert len(store.read_after(SPEC.plan_id, None)) == 1


def test_profile_is_immutable_and_snapshot_schema_requires_the_pair(store):
    spec = replace(SPEC, plan_id="profile-plan", profile_id="full", profile_version="1.0")
    store.register_plan(spec, run_id="run-profile", occurred_at=TIME)
    store.register_plan(spec, run_id="run-retry", occurred_at=TIME)
    schema = json.loads(files("bravo_platform_events").joinpath("schemas/plan-snapshot.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    snapshot = store.snapshot(spec.plan_id)
    validator.validate(snapshot)
    assert snapshot["profile_id"] == "full" and snapshot["profile_version"] == "1.0"
    for field in ("profile_id", "profile_version"):
        invalid = dict(snapshot)
        invalid.pop(field)
        assert not validator.is_valid(invalid)
        with pytest.raises(EventError, match="plan_conflict"):
            store.register_plan(replace(spec, **{field: "different"}), run_id="run-retry", occurred_at=TIME)
    assert store.snapshot(spec.plan_id) == snapshot
    assert len(store.read_after(spec.plan_id, None)) == 1


@pytest.mark.parametrize("change", [
    dict(profile_id="full"), dict(profile_version="1"),
    dict(profile_id="full", profile_version=1),
    dict(profile_id="full", profile_version="unsafe value"),
    dict(recheck_profile_id="apply-recheck", recheck_profile_version="1"),
    dict(profile_id="demo-gate", profile_version="1", recheck_profile_id="apply-recheck"),
    dict(profile_id="demo-gate", profile_version="1", recheck_profile_version="1"),
])
def test_partial_or_invalid_profile_never_registers(store, change):
    spec = replace(SPEC, plan_id="bad-profile-plan", **change)
    with pytest.raises(ValueError, match="invalid_evaluation_profile"):
        store.register_plan(spec, run_id="run-profile", occurred_at=TIME)
    with pytest.raises(EventError, match="plan_not_found"):
        store.snapshot(spec.plan_id)


def test_recheck_snapshot_requires_initial_and_recheck_pairs_and_preserves_old_bytes(store):
    with store._transaction() as c:
        old = c.execute("SELECT spec_json FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone()["spec_json"]
    store.register_plan(SPEC, run_id="retry", occurred_at=TIME)
    with store._transaction() as c:
        assert c.execute("SELECT spec_json FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone()["spec_json"] == old
    spec = replace(SPEC, plan_id="recheck-plan", profile_id="demo-gate", profile_version="1",
                   recheck_profile_id="apply-recheck", recheck_profile_version="1")
    store.register_plan(spec, run_id="recheck", occurred_at=TIME)
    schema = json.loads(files("bravo_platform_events").joinpath("schemas/plan-snapshot.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    snapshot = store.snapshot(spec.plan_id)
    validator.validate(snapshot)
    for field in ("profile_id", "profile_version", "recheck_profile_id", "recheck_profile_version"):
        invalid = dict(snapshot)
        invalid.pop(field)
        assert not validator.is_valid(invalid)
        with pytest.raises(EventError, match="plan_conflict"):
            store.register_plan(replace(spec, **{field: "changed"}), run_id="retry", occurred_at=TIME)


@pytest.mark.parametrize(
    "change,error",
    [
        (dict(proof=replace(PROOF, complete=False)), "collection_incomplete"),
        (dict(proof=replace(PROOF, complete=1)), "collection_incomplete"),
        (dict(proof=replace(PROOF, plan_digest="c" * 64)), "collection_context_mismatch"),
        (
            dict(proof=replace(PROOF, collection_scope_digest="c" * 64)),
            "collection_context_mismatch",
        ),
        (dict(proof=replace(PROOF, plan_id="unknown")), "plan_not_found"),
        (dict(expected_version=2), "version_conflict"),
        (dict(expected_version=True), "invalid_version"),
    ],
)
def test_unsatisfied_conditions_do_not_write(store, change, error):
    before = store.snapshot(SPEC.plan_id)
    with pytest.raises(ValueError, match=error):
        complete(store, **change)
    assert store.snapshot(SPEC.plan_id) == before
    assert len(store.read_after(SPEC.plan_id, None)) == 1


def test_no_second_or_arbitrary_transition(store):
    complete(store)
    with pytest.raises(EventError, match="transition_not_allowed"):
        complete(store, expected_version=2, command_key="collect-2")
    # Even another EventStore handle must not bypass the managed state writer.
    plain = EventStore(store.path)
    with pytest.raises(EventError, match="managed_state_required"):
        plain.append(
            plan_id=SPEC.plan_id,
            run_id="run-2",
            producer_key="unsafe",
            event_type="state.changed",
            data={"from": "ANALYZE", "to": "DONE"},
            occurred_at=TIME,
        )
    assert store.snapshot(SPEC.plan_id)["state"] == "ANALYZE"


def test_registration_cannot_adopt_an_existing_event_stream(tmp_path):
    plain = EventStore(tmp_path / "old.db")
    plain.register_stream(SPEC.plan_id)
    plans = PlanStore(plain.path)
    with pytest.raises(EventError, match="stream_conflict"):
        plans.register_plan(SPEC, run_id="run-1", occurred_at=TIME)
    assert plans.tenant_for(SPEC.plan_id) is None


@pytest.mark.parametrize(
    "change",
    [
        dict(expected_version=2),
        dict(run_id="run-2"),
        dict(occurred_at="2026-11-03T10:00:01Z"),
        dict(proof=replace(PROOF, evidence_id="other")),
    ],
)
def test_command_key_binds_all_request_inputs(store, change):
    complete(store)
    with pytest.raises(EventError, match="command_key_conflict"):
        complete(store, **change)
    assert len(store.read_after(SPEC.plan_id, None)) == 2


def test_competing_commands_have_one_winner_and_one_event(store):
    barrier = Barrier(8)

    def compete(i):
        barrier.wait(timeout=5)
        try:
            return complete(store, command_key=f"command-{i}")
        except EventError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 7
    assert len(store.read_after(SPEC.plan_id, None)) == 2


def test_concurrent_retries_return_same_receipt(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: complete(store), range(16)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(store.read_after(SPEC.plan_id, None)) == 2


@pytest.mark.parametrize("table", ["events", "plan_commands"])
def test_failure_between_state_event_and_receipt_rolls_back_everything(store, table):
    before = store.snapshot(SPEC.plan_id)
    with sqlite3.connect(store.path) as c:
        c.execute(
            f"CREATE TRIGGER fault BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        complete(store)
    assert store.snapshot(SPEC.plan_id) == before
    assert len(store.read_after(SPEC.plan_id, None)) == 1
    with sqlite3.connect(store.path) as c:
        c.execute("DROP TRIGGER fault")
    assert complete(store)["event_id"] == "plan-demo:2"


def test_invalid_event_rolls_back_registration_and_transition(tmp_path, store):
    empty = PlanStore(tmp_path / "empty.db")
    with pytest.raises(ValidationError):
        empty.register_plan(SPEC, run_id="run-1", occurred_at="invalid")
    assert empty.tenant_for(SPEC.plan_id) is None
    empty.register_plan(SPEC, run_id="run-1", occurred_at=TIME)
    assert empty.snapshot(SPEC.plan_id)["last_event_id"] == "plan-demo:1"
    with pytest.raises(ValidationError):
        complete(store, occurred_at="invalid")
    assert store.snapshot(SPEC.plan_id)["state"] == "COLLECT"
    assert complete(store)["event_id"] == "plan-demo:2"


def test_snapshot_is_consistent_when_writer_commits_between_reads(store, monkeypatch):
    original = store._snapshot

    def overlap(c, plan):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(complete, store).result(timeout=5)["event_id"] == "plan-demo:2"
        return original(c, plan)

    monkeypatch.setattr(store, "_snapshot", overlap)
    snapshot = store.snapshot(SPEC.plan_id)
    assert (snapshot["state"], snapshot["version"], snapshot["last_event_id"]) == (
        "COLLECT",
        1,
        "plan-demo:1",
    )
    monkeypatch.setattr(store, "_snapshot", original)
    # The committed transition is still available immediately after the watermark.
    assert store.read_after(SPEC.plan_id, snapshot["last_event_id"])[0]["data"]["to"] == "ANALYZE"


def test_snapshot_watermark_includes_other_committed_event_types(store):
    complete(store)
    progress(store)
    snapshot = store.snapshot(SPEC.plan_id)
    assert (snapshot["state"], snapshot["version"], snapshot["last_event_id"]) == (
        "ANALYZE",
        2,
        "plan-demo:3",
    )
    assert store.read_after(SPEC.plan_id, snapshot["last_event_id"]) == []


def test_snapshot_then_sse_has_no_missed_state_change(store):
    client = TestClient(
        create_plan_app(store, resolve_principal=lambda _: Principal("user-demo", "tenant-demo"))
    )
    response = client.get("/v1/plans/plan-demo")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    complete(store)
    resumed = client.get(
        "/v1/plans/plan-demo/events?follow=false",
        headers={"Last-Event-ID": response.json()["last_event_id"]},
    )
    events = [
        json.loads(line[6:]) for line in resumed.text.splitlines() if line.startswith("data: ")
    ]
    assert [(event["event_id"], event["data"]["to"]) for event in events] == [
        ("plan-demo:2", "ANALYZE")
    ]
    assert client.post("/v1/plans", json={"complete": True}).status_code == 404
    assert client.post("/v1/plans/plan-demo", json={"state": "DONE"}).status_code == 405


@pytest.mark.parametrize(
    "principal",
    [
        None,
        True,
        {"tenant_id": "tenant-demo"},
        Principal("user-demo", "other"),
        Principal("", "tenant-demo"),
    ],
)
def test_same_deny_policy_for_snapshot_and_events(store, principal):
    client = TestClient(create_plan_app(store, resolve_principal=lambda _: principal))
    for path in (
        "plan-demo",
        "plan-demo/events?follow=false",
        "missing",
        "missing/events?follow=false",
    ):
        response = client.get(
            f"/v1/plans/{path}", headers={"X-Tenant-ID": "tenant-demo", "Last-Event-ID": "bad"}
        )
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "forbidden"}}


def test_default_is_unauthenticated_and_unknown_plan_does_not_leak(store):
    assert TestClient(create_plan_app(store)).get("/v1/plans/plan-demo").status_code == 403
    client = TestClient(
        create_plan_app(store, resolve_principal=lambda _: Principal("user-demo", "tenant-demo"))
    )
    assert client.get("/v1/plans/unknown").status_code == 403
    assert client.get("/v1/plans/unknown/events?follow=false").status_code == 403


def test_revocation_during_snapshot_read_is_checked_again(store, monkeypatch):
    active = True
    original = store.snapshot

    def revoke(plan_id):
        nonlocal active
        result = original(plan_id)
        active = False
        return result

    monkeypatch.setattr(store, "snapshot", revoke)
    client = TestClient(
        create_plan_app(
            store,
            resolve_principal=lambda _: Principal("user-demo", "tenant-demo") if active else None,
        )
    )
    assert client.get("/v1/plans/plan-demo").status_code == 403


def test_snapshot_does_not_claim_audit_history_recovery(store):
    complete(store)
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM events")
    assert store.snapshot(SPEC.plan_id)["last_event_id"] == "plan-demo:2"
    with pytest.raises(EventError, match="resync_required"):
        store.read_after(SPEC.plan_id, "plan-demo:2")


def test_published_snapshot_schema_and_example_match_local_read_model(store):
    schemas = files("bravo_platform_events").joinpath("schemas")
    schema = json.loads(schemas.joinpath("plan-snapshot.schema.json").read_text())
    events = json.loads(schemas.joinpath("event.schema.json").read_text())
    assert set(schema["properties"]["state"]["enum"]) == set(events["$defs"]["state"]["enum"])
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.check_schema(schema)
    example = json.loads((Path(__file__).parents[1] / "examples/plan-snapshot.json").read_text())
    assert store.snapshot(SPEC.plan_id) == example
    validator.validate(example)
    complete(store)
    validator.validate(store.snapshot(SPEC.plan_id))
    for invalid in [
        dict(example, tenant_id="other"),
        dict(example, state="INVALID_STATE"),
        dict(example, version=True),
    ]:
        with pytest.raises(ValidationError):
            validator.validate(invalid)
