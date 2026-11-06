"""One attempt's tool events, exported complete or not at all. No LLM client is imported.

The coordinator's ``audit`` holds code-owned values only (tool names, outcomes, reasons,
categories, digests), never model arguments. This module is the single extractor and validator
that T15 (rehearsal runner), T38 (product path) and T24/T21 can share: an attempt's events are
returned in their original order, without the independent verifier's own record, and only when
the coordinator says the record is complete. Anything doubtful raises; it is never filtered.
"""

import re
from collections.abc import Mapping

# T15 ``collect_tool_events`` refuses more than 256 events; one attempt keeps at most 128 tool
# events plus coordinator stop events, so a complete attempt always fits.
MAX_TOOL_EVENTS = 256
MAX_EVENT_FIELDS = 16
MAX_TEXT = 256
# JSON-safe integers (T10/T21 parse the array in JavaScript).
MAX_INT = 2**53 - 1
# The verifier record judges the attempt; it is not something the model's tools did.
EXCLUDED_TOOLS = frozenset({"independent_verifier"})
REQUIRED_FIELDS = ("run_id", "attempt", "tool", "outcome", "reason")

RUN_ID = r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"  # contracts.RunId
FIELD = r"[a-z][a-z0-9_]{0,31}"
TOOL = r"[a-z][a-z0-9_.]{0,63}"
CODE = r"[A-Z][A-Z0-9_]{0,63}"
TEXT = rf"[ -~]{{0,{MAX_TEXT}}}"  # printable ASCII; no control characters or newlines


def _text(pattern: str) -> dict:
    return {"type": "string", "pattern": f"^{pattern}$"}


# The per-event shape as JSON Schema (draft 2020-12) for consumers that validate declaratively.
# ``validate_tool_events`` enforces the same rules and additionally binds run_id/attempt and
# rejects non-integral numbers, which JSON Schema's "integer" would accept (1.0).
TOOL_EVENT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": list(REQUIRED_FIELDS),
    "maxProperties": MAX_EVENT_FIELDS,
    "propertyNames": {"pattern": f"^{FIELD}$"},
    "properties": {
        "run_id": _text(RUN_ID),
        "attempt": {"type": "integer", "minimum": 1, "maximum": MAX_INT},
        "tool": {**_text(TOOL), "not": {"enum": sorted(EXCLUDED_TOOLS)}},
        "outcome": _text(CODE),
        "reason": _text(CODE),
    },
    "additionalProperties": {"anyOf": [
        _text(TEXT),
        {"type": "integer", "minimum": -MAX_INT, "maximum": MAX_INT},
        {"type": "boolean"},
    ]},
}
TOOL_EVENTS_SCHEMA = {
    "$schema": TOOL_EVENT_SCHEMA["$schema"],
    "type": "array",
    "maxItems": MAX_TOOL_EVENTS,
    "items": {key: value for key, value in TOOL_EVENT_SCHEMA.items() if key != "$schema"},
}


class ToolEventsUnavailable(ValueError):
    """The attempt's events cannot be exported complete; report ``UNAVAILABLE`` instead."""


def _match(pattern: str, value) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _scalar(value) -> bool:
    if type(value) is bool:
        return True
    if type(value) is int:
        return -MAX_INT <= value <= MAX_INT
    return _match(TEXT, value)


def validate_tool_events(events, run_id: str, attempt: int) -> tuple[dict, ...]:
    """Check an exported event list against ``TOOL_EVENTS_SCHEMA`` and bind every event to
    ``run_id``/``attempt``. Returns detached copies; raises ``ToolEventsUnavailable``."""
    if not _match(RUN_ID, run_id) or type(attempt) is not int or not 1 <= attempt <= MAX_INT:
        raise ToolEventsUnavailable("invalid run or attempt")
    if not isinstance(events, (list, tuple)) or len(events) > MAX_TOOL_EVENTS:
        raise ToolEventsUnavailable("tool events outside bounds")
    checked = []
    for event in events:
        if (type(event) is not dict or len(event) > MAX_EVENT_FIELDS
                or any(not _match(FIELD, key) for key in event)
                or any(key not in event for key in REQUIRED_FIELDS)):
            raise ToolEventsUnavailable("tool event shape")
        if (event["run_id"] != run_id or type(event["attempt"]) is not int
                or event["attempt"] != attempt):
            raise ToolEventsUnavailable("tool event from another run or attempt")
        if (not _match(TOOL, event["tool"]) or event["tool"] in EXCLUDED_TOOLS
                or not _match(CODE, event["outcome"]) or not _match(CODE, event["reason"])
                or not all(_scalar(value) for value in event.values())):
            raise ToolEventsUnavailable("tool event value")
        checked.append(dict(event))
    return tuple(checked)


def _result_of(source) -> Mapping:
    result = getattr(source, "result", None)
    if not isinstance(source, Mapping) and callable(result):
        source = result()
    if not isinstance(source, Mapping):
        raise ToolEventsUnavailable("coordinator result required")
    return source


def attempt_tool_events(source, attempt: int, *, run_id: str | None = None) -> tuple[dict, ...]:
    """The canonical extractor of one attempt's tool events.

    ``source`` is a live ``Coordinator`` or its ``result()`` dict (also after a JSON round
    trip, which is what the T38 product path receives from the worker). Events are those of
    ``attempt`` except the independent verifier's, in their original order. The attempt must
    have exactly one record with ``audit_truncated is False`` and ``audit_event_count`` equal to
    the number of those events; coordinator stop events (``tool=coordinator``) count toward it.
    ``run_id``, when given, must be the result's run. Raises ``ToolEventsUnavailable``.
    """
    result = _result_of(source)
    bound = result.get("run_id")
    if run_id is not None and bound != run_id:
        raise ToolEventsUnavailable("result belongs to another run")
    if type(attempt) is not int or attempt < 1:
        raise ToolEventsUnavailable("invalid attempt")
    attempts, audit = result.get("attempts"), result.get("audit")
    if (not isinstance(attempts, (list, tuple)) or not isinstance(audit, (list, tuple))
            or any(not isinstance(item, Mapping) for item in (*attempts, *audit))):
        raise ToolEventsUnavailable("attempt records missing")
    records = [record for record in attempts if record.get("attempt") == attempt]
    if (len(records) != 1 or type(records[0].get("attempt")) is not int
            or records[0].get("audit_truncated") is not False
            or type(records[0].get("audit_event_count")) is not int):
        raise ToolEventsUnavailable("tool events unavailable")
    # Select by attempt only; a foreign run_id or a malformed event is refused below, not skipped.
    events = [dict(event) for event in audit
              if event.get("attempt") == attempt and event.get("tool") not in EXCLUDED_TOOLS]
    if len(events) != records[0]["audit_event_count"]:
        raise ToolEventsUnavailable("tool events incomplete")
    return validate_tool_events(events, bound, attempt)


def tool_events_projection(source, attempt: int, *, run_id: str | None = None) -> dict:
    """The response fields proposed for T38 (not wired there): either
    ``{"tool_events_status": "COMPLETE", "tool_events": [...]}`` or
    ``{"tool_events_status": "UNAVAILABLE"}``. Any doubt drops the whole list."""
    try:
        events = attempt_tool_events(source, attempt, run_id=run_id)
    except ToolEventsUnavailable:
        return {"tool_events_status": "UNAVAILABLE"}
    return {"tool_events_status": "COMPLETE", "tool_events": list(events)}
