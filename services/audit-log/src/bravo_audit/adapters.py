"""Map upstream events to (source, kind, payload) without inventing new event types."""

from __future__ import annotations

PLATFORM_EVENT_TYPES = frozenset({"state.changed", "approval.requested", "approval.decided",
                                  "gate.completed", "execution.updated", "failback.recorded"})
TOOL_OUTCOMES = frozenset({"ALLOWED", "DENIED", "OBSERVED"})


def from_platform_event(event: dict) -> tuple[str, str, str, str, str, dict]:
    """T10 plan event (contract v1) → (plan_id, run_id, source, kind, origin_id, payload).
    ``origin_id`` is the T10 ``event_id`` so a redelivered event is recognised."""
    required = {"schema_version", "event_id", "sequence", "plan_id", "run_id", "occurred_at", "event_type", "data"}
    if not required <= set(event):
        raise ValueError("platform event missing required fields")
    if event["event_type"] not in PLATFORM_EVENT_TYPES:
        raise ValueError("unknown platform event type")
    if event["event_id"] != f"{event['plan_id']}:{event['sequence']}":
        raise ValueError("platform event id does not bind plan and sequence")
    payload = {k: event[k] for k in ("schema_version", "event_id", "sequence", "occurred_at", "event_type", "data")}
    return event["plan_id"], event["run_id"], "platform.events", event["event_type"], f"t10:{event['event_id']}", payload


def from_agent_tool_event(event: dict, plan_id: str, index: int) -> tuple[str, str, str, str, str, dict]:
    """T11 ``Tools.events`` entry → (plan_id, run_id, source, kind, origin_id, payload).
    Tool events carry no id of their own; ``attempt`` plus the position in the attempt's event
    list identifies one event, so a re-submitted attempt log does not duplicate records.

    ``kind`` is the code-owned reason (e.g. ``DESTRUCTIVE_ATTEMPT_BLOCKED``) so the console can
    find the design 8.4 demo scene without reading payloads. Raw model arguments never reach
    these events in the first place (agent-worker policy)."""
    required = {"run_id", "attempt", "tool", "outcome", "reason"}
    if not required <= set(event):
        raise ValueError("tool event missing required fields")
    if event["outcome"] not in TOOL_OUTCOMES:
        raise ValueError("unknown tool outcome")
    if not isinstance(index, int) or index < 0:
        raise ValueError("tool event index required")
    payload = {k: v for k, v in event.items() if k != "run_id"}
    origin = f"t11:{event['run_id']}:attempt{int(event['attempt'])}:{index}"
    return plan_id, event["run_id"], "agent.tools", str(event["reason"]), origin, payload


def expand_platform_event(event: dict) -> list[tuple[str, str, str, str, str, dict]]:
    """A T10 event plus the T11 tool events it forwards in ``data.tool_events``.

    T15 signs one attempt's T11 events and T10 forwards the array unchanged (rehearsal-runner
    ``collect_tool_events``). Each element keeps its *original* index in that array, so the
    ``origin_id`` matches what a direct T11 submission of the same attempt would produce and a
    redelivery through either path is absorbed. The platform record keeps only the count and a
    digest of the array, not a second copy. An array whose status is not ``COMPLETE`` is not
    split: a partial list must not look like the whole attempt."""
    import hashlib
    import json

    head = from_platform_event(event)
    data = event["data"]
    if not isinstance(data, dict) or "tool_events" not in data:
        return [head]
    tools = data["tool_events"]
    if not isinstance(tools, list) or data.get("tool_events_status", "COMPLETE") != "COMPLETE":
        raise ValueError("tool_events must be a complete list")
    plan_id, run_id, source, kind, origin, payload = head
    digest = hashlib.sha256(json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    summary = {k: v for k, v in data.items() if k != "tool_events"} | {
        "tool_events_count": len(tools), "tool_events_sha256": digest}
    out = [(plan_id, run_id, source, kind, origin, payload | {"data": summary})]
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or tool.get("run_id") != run_id:
            raise ValueError("forwarded tool event does not belong to this run")
        out.append(from_agent_tool_event(tool, plan_id, index))
    return out
