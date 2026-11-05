"""Map upstream events to (source, kind, payload) without inventing new event types."""

from __future__ import annotations

PLATFORM_EVENT_TYPES = frozenset({"state.changed", "approval.requested", "approval.decided",
                                  "gate.completed", "execution.updated", "failback.recorded"})
TOOL_OUTCOMES = frozenset({"ALLOWED", "DENIED", "OBSERVED"})


def from_platform_event(event: dict) -> tuple[str, str, str, str, dict]:
    """T10 plan event (contract v1) → (plan_id, run_id, source, kind, payload)."""
    required = {"schema_version", "event_id", "sequence", "plan_id", "run_id", "occurred_at", "event_type", "data"}
    if not required <= set(event):
        raise ValueError("platform event missing required fields")
    if event["event_type"] not in PLATFORM_EVENT_TYPES:
        raise ValueError("unknown platform event type")
    if event["event_id"] != f"{event['plan_id']}:{event['sequence']}":
        raise ValueError("platform event id does not bind plan and sequence")
    payload = {k: event[k] for k in ("schema_version", "event_id", "sequence", "occurred_at", "event_type", "data")}
    return event["plan_id"], event["run_id"], "platform.events", event["event_type"], payload


def from_agent_tool_event(event: dict, plan_id: str) -> tuple[str, str, str, str, dict]:
    """T11 ``Tools.events`` entry → (plan_id, run_id, source, kind, payload).

    ``kind`` is the code-owned reason (e.g. ``DESTRUCTIVE_ATTEMPT_BLOCKED``) so the console can
    find the design 8.4 demo scene without reading payloads. Raw model arguments never reach
    these events in the first place (agent-worker policy)."""
    required = {"run_id", "attempt", "tool", "outcome", "reason"}
    if not required <= set(event):
        raise ValueError("tool event missing required fields")
    if event["outcome"] not in TOOL_OUTCOMES:
        raise ValueError("unknown tool outcome")
    payload = {k: v for k, v in event.items() if k != "run_id"}
    return plan_id, event["run_id"], "agent.tools", str(event["reason"]), payload
