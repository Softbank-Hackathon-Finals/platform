"""Simulate T15→T10 approval binding. FakeReceiver is not an approval authority."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bravo_platform_events.contract import validate_event

EVENTS = Path(__file__).parents[2] / "platform-events/examples/events.json"


def change_event():
    event = next(x for x in json.loads(EVENTS.read_text()) if x["event_type"] == "approval.requested")
    event["data"]["approval_kind"] = "change"
    del event["data"]["binding"]["gate_id"]
    del event["data"]["binding"]["context_digest"]
    return event


class FakeReceiver:
    """A test double for current values; not T10's unimplemented change approval path."""

    def __init__(self, current):
        self.current = deepcopy(current)
        self.accepted = []

    def receive(self, event, *, verified, cleanup_confirmed):
        validate_event(event)
        if verified is not True or cleanup_confirmed is not True:
            raise ValueError("verification or cleanup unknown")
        if event["data"]["binding"] != self.current:
            raise ValueError("binding mismatch")
        self.accepted.append(deepcopy(event))


def test_matching_synthetic_binding_reaches_mock_only():
    event = change_event()
    receiver = FakeReceiver(event["data"]["binding"])
    receiver.receive(event, verified=True, cleanup_confirmed=True)
    assert receiver.accepted == [event]


@pytest.mark.parametrize("field", ["diff_hash", "image_digest"])
def test_changed_artifact_is_rejected_by_mock_current_values(field):
    event = change_event()
    receiver = FakeReceiver(event["data"]["binding"])
    event["data"]["binding"][field] = "sha256:" + "f" * 64
    # Wire schema checks encoding, not whether the artifact matches current values.
    validate_event(event)
    with pytest.raises(ValueError, match="binding mismatch"):
        receiver.receive(event, verified=True, cleanup_confirmed=True)
    assert receiver.accepted == []


@pytest.mark.parametrize("verified,cleanup", [(False, True), (None, True), (True, False), (True, None)])
def test_mock_rejects_unverified_or_uncleaned(verified, cleanup):
    event = change_event()
    receiver = FakeReceiver(event["data"]["binding"])
    with pytest.raises(ValueError, match="verification or cleanup unknown"):
        receiver.receive(event, verified=verified, cleanup_confirmed=cleanup)
    assert receiver.accepted == []


def test_wire_schema_cannot_replace_current_binding_check():
    original = change_event()
    stale = deepcopy(original)
    stale["data"]["binding"]["image_digest"] = "sha256:" + "f" * 64
    validate_event(stale)
    assert stale["data"]["binding"] != original["data"]["binding"]
