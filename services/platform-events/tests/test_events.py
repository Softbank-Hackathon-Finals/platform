import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from bravo_platform_events.api import create_app, event_stream
from bravo_platform_events.contract import SCHEMA, VALIDATOR, encode_sse, validate_event
from bravo_platform_events.store import EventError, EventStore

EXAMPLES = json.loads((Path(__file__).parents[1] / "examples/events.json").read_text())


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    s.register_stream("plan-demo")
    s.register_stream("plan-other")
    return s


def append(store, key="source-1", *, plan="plan-demo", run="run-1", state="COLLECT"):
    return store.append(
        plan_id=plan,
        run_id=run,
        producer_key=key,
        event_type="state.changed",
        data={"from": None, "to": state},
        occurred_at="2026-11-03T10:00:00Z",
    )


def test_schema_and_published_examples():
    VALIDATOR.check_schema(SCHEMA)
    for event in EXAMPLES:
        validate_event(event)
        wire = encode_sse(event)
        assert wire.endswith("\n\n")
        assert wire.splitlines()[0] == f"id: {event['event_id']}"
        assert json.loads(wire.splitlines()[2].removeprefix("data: ")) == event


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(schema_version="2.0"),
        lambda e: e.update(sequence=0),
        lambda e: e.update(sequence=True),
        lambda e: e.update(event_type="state.changed\nevent: execute"),
        lambda e: e.update(occurred_at="2026-11-03T10:00:00"),
        lambda e: e.update(extra="not-permitted"),
        lambda e: e["data"].update(to="SILENT_PASS"),
        lambda e: e["data"].update(credentials="forbidden-property"),
    ],
)
def test_unknown_or_unsafe_wire_data_is_rejected(mutation):
    event = deepcopy(EXAMPLES[0])
    mutation(event)
    with pytest.raises(ValidationError):
        validate_event(event)


def test_event_identity_and_approval_plan_cannot_disagree():
    event = deepcopy(EXAMPLES[0])
    event["event_id"] = "plan-other:1"
    with pytest.raises(ValueError, match="event_id_mismatch"):
        validate_event(event)
    approval = deepcopy(EXAMPLES[2])
    approval["data"]["binding"]["plan_id"] = "plan-other"
    with pytest.raises(ValueError, match="binding_plan_mismatch"):
        validate_event(approval)


def test_cutover_requires_gate_and_all_binding_values():
    for missing in [
        "plan_digest",
        "diff_hash",
        "image_digest",
        "target_environment_id",
        "expires_at",
        "gate_id",
        "context_digest",
    ]:
        approval = deepcopy(EXAMPLES[2])
        del approval["data"]["binding"][missing]
        with pytest.raises(ValidationError):
            validate_event(approval)


@pytest.mark.parametrize("fixture", ["gate-before-fix.json", "gate-after-fix.json"])
def test_gate_summary_preserves_current_t5_result_fields(fixture):
    gate = json.loads((Path(__file__).parents[3] / "docs/controls/examples" / fixture).read_text())
    event = deepcopy(EXAMPLES[1])
    event["data"].update({key: gate[key] for key in ("gate_id", "context_digest", "decision")})
    validate_event(event)
    assert json.loads(encode_sse(event).splitlines()[2].removeprefix("data: ")) == event


@pytest.mark.parametrize("decision", ["UNKNOWN", "FAIL"])
def test_raw_test_verdict_is_not_a_gate_decision(decision):
    event = deepcopy(EXAMPLES[1])
    event["data"]["decision"] = decision
    with pytest.raises(ValidationError):
        validate_event(event)


@pytest.mark.parametrize("digest", ["", "sha256:" + "4" * 64, "A" * 64])
def test_gate_context_requires_t5_digest_encoding(digest):
    event = deepcopy(EXAMPLES[1])
    event["data"]["context_digest"] = digest
    with pytest.raises(ValidationError):
        validate_event(event)


@pytest.mark.parametrize("missing", ["gate_id", "context_digest"])
def test_optional_change_approval_gate_reference_must_be_complete(missing):
    event = deepcopy(EXAMPLES[2])
    event["data"]["approval_kind"] = "change"
    del event["data"]["binding"][missing]
    with pytest.raises(ValidationError):
        validate_event(event)


def test_approval_history_can_contain_expired_values_without_authorizing_execution():
    event = deepcopy(EXAMPLES[3])
    event["data"]["decision"] = "expired"
    event["data"]["binding"]["expires_at"] = "2026-01-01T00:00:00Z"
    validate_event(event)
    # Wire validity preserves historical records; effect authorization is not this module's job.
    assert "execute" not in SCHEMA["properties"]


def test_producer_retry_and_conflict_do_not_advance_sequence(store):
    first = append(store)
    assert append(store) == first
    with pytest.raises(EventError, match="producer_key_conflict"):
        append(store, state="ANALYZE")
    assert append(store, "source-2", state="ANALYZE")["sequence"] == 2


def test_validation_failure_rolls_back_sequence(store):
    with pytest.raises(ValidationError):
        append(store, state="UNKNOWN_STATE")
    assert append(store)["sequence"] == 1


def test_restart_resume_and_run_change_preserve_plan_order(store):
    first = append(store)
    second = append(store, "source-2", run="run-2", state="ANALYZE")
    resumed = EventStore(store.path)
    assert resumed.read_after("plan-demo", first["event_id"]) == [second]
    assert resumed.read_after("plan-other", None) == []


def test_concurrent_writers_get_committed_contiguous_sequences(store):
    def produce(n):
        return append(EventStore(store.path), f"source-{n}")["sequence"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        sequences = list(pool.map(produce, range(24)))
    assert sorted(sequences) == list(range(1, 25))
    assert [e["sequence"] for e in store.read_after("plan-demo", None)] == list(range(1, 25))


@pytest.mark.parametrize(
    "cursor",
    [
        "plan-other:1",
        "plan-demo:0",
        "plan-demo:01",
        "1",
        "plan-demo:1\n",
        "plan-demo:9007199254740992",
    ],
)
def test_cross_plan_or_malformed_cursor_is_not_accepted(store, cursor):
    append(store)
    with pytest.raises(EventError, match="invalid_cursor"):
        store.read_after("plan-demo", cursor)


def test_future_or_pruned_cursor_requires_explicit_resync(store):
    append(store)
    second = append(store, "source-2")
    with pytest.raises(EventError, match="resync_required"):
        store.read_after("plan-demo", "plan-demo:3")
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM events WHERE plan_id='plan-demo' AND sequence=1")
    for cursor in [None, "plan-demo:1"]:
        with pytest.raises(EventError, match="resync_required"):
            store.read_after("plan-demo", cursor)
    assert store.read_after("plan-demo", second["event_id"]) == []


def test_missing_tail_or_all_pruned_events_cannot_look_like_empty_stream(store):
    append(store)
    append(store, "source-2")
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM events WHERE plan_id='plan-demo' AND sequence=2")
    with pytest.raises(EventError, match="resync_required"):
        store.read_after("plan-demo", "plan-demo:1")
    with pytest.raises(EventError, match="resync_required"):
        store.read_after("plan-demo", None)
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM events WHERE plan_id='plan-demo'")
    with pytest.raises(EventError, match="resync_required"):
        store.read_after("plan-demo", None)


def test_http_reconnect_replays_only_later_events_and_preserves_read_boundary(store):
    first = append(store)
    second = append(store, "source-2", state="ANALYZE")
    client = TestClient(create_app(store, authorize=lambda _r, p: p == "plan-demo"))
    response = client.get(
        "/v1/plans/plan-demo/events?follow=false", headers={"Last-Event-ID": first["event_id"]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == "retry: 1000\n\n" + encode_sse(second)
    assert client.post("/v1/plans/plan-demo/events").status_code == 405
    assert len(store.read_after("plan-demo", None)) == 2


def test_http_denies_before_revealing_plan_or_cursor_details(store):
    client = TestClient(create_app(store))
    for plan in ["plan-demo", "unknown-plan"]:
        response = client.get(
            f"/v1/plans/{plan}/events?follow=false", headers={"Last-Event-ID": "bad-cursor"}
        )
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "forbidden"}}


def test_ambiguous_and_async_authorizers_cannot_allow_access(store):
    client = TestClient(create_app(store, authorize=lambda _r, _p: "allowed"))
    assert client.get("/v1/plans/plan-demo/events?follow=false").status_code == 403

    async def async_authorizer(_r, _p):
        return True

    with pytest.raises(ValueError, match="synchronous"):
        create_app(store, authorize=async_authorizer)


@pytest.mark.parametrize(
    ("plan", "cursor", "status", "code"),
    [
        ("plan-demo", "plan-other:1", 400, "invalid_cursor"),
        ("plan-demo", "plan-demo:99", 409, "resync_required"),
        ("unknown", None, 404, "plan_not_found"),
    ],
)
def test_http_resume_failures_are_explicit(store, plan, cursor, status, code):
    append(store)
    client = TestClient(create_app(store, authorize=lambda _r, _p: True))
    response = client.get(
        f"/v1/plans/{plan}/events?follow=false", headers={"Last-Event-ID": cursor} if cursor else {}
    )
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_live_stream_sees_later_commit_and_closes_after_access_revocation(store):
    class Request:
        async def is_disconnected(self):
            return False

    allowed = True

    async def scenario():
        nonlocal allowed
        stream = event_stream(
            store,
            Request(),
            "plan-demo",
            None,
            lambda _r, _p: allowed,
            follow=True,
            poll_seconds=0.001,
        )
        assert await anext(stream) == "retry: 1000\n\n"
        assert await anext(stream) == ": keep-alive\n\n"
        event = append(store)
        assert await anext(stream) == encode_sse(event)
        allowed = False
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(scenario())
