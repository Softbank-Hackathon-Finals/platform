import itertools

import pytest

from bravo_audit.ledger import RunLedger


def ticking():
    counter = itertools.count(1)
    return lambda: f"2026-11-05T10:00:{next(counter):02d}Z"


@pytest.fixture
def ledger():
    return RunLedger("plan-demo", "run-demo-1", now=ticking())


@pytest.fixture
def fresh_ledger():
    return lambda: RunLedger("plan-demo", "run-demo-1", now=ticking())


PLATFORM_EVENTS = [
    {"schema_version": "1.0", "event_id": "plan-demo:1", "sequence": 1, "plan_id": "plan-demo", "run_id": "run-demo-1",
     "occurred_at": "2026-11-05T09:59:01Z", "event_type": "state.changed", "data": {"from": None, "to": "COLLECT"}},
    {"schema_version": "1.0", "event_id": "plan-demo:2", "sequence": 2, "plan_id": "plan-demo", "run_id": "run-demo-1",
     "occurred_at": "2026-11-05T09:59:02Z", "event_type": "gate.completed",
     "data": {"gate_id": "gate-1", "evidence_ref": "evidence-1", "decision": "BLOCK", "context_digest": "4" * 64}},
]

TOOL_EVENTS = [
    {"run_id": "run-demo-1", "attempt": 1, "tool": "repo.read", "outcome": "ALLOWED", "reason": "POLICY_ACCEPTED",
     "path": "app/db_secret.py", "content_digest": "a" * 64},
    {"run_id": "run-demo-1", "attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
     "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"},
]


@pytest.fixture
def platform_events():
    return [dict(e) for e in PLATFORM_EVENTS]


@pytest.fixture
def tool_events():
    return [dict(e) for e in TOOL_EVENTS]
