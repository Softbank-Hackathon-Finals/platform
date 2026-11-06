import ast
import asyncio
import copy
import json
from pathlib import Path

import pytest

from bravo_agent import tool_events as module
from bravo_agent.contracts import VerificationReceipt
from bravo_agent.coordinator import Coordinator
from bravo_agent.openai_provider import OpenAIProvider
from bravo_agent.tool_events import (
    MAX_TOOL_EVENTS,
    TOOL_EVENTS_SCHEMA,
    ToolEventsUnavailable,
    attempt_tool_events,
    tool_events_projection,
    validate_tool_events,
)
from bravo_agent.tools import PolicyDenied

BUCKET_DELETE = {"command": "aws s3 rb s3://private-bucket --force"}


def t15_attempt_tool_events(coordinator, attempt):
    """Reference oracle: a verbatim copy of T15's ``attempt_tool_events`` in
    services/rehearsal-runner/src/rehearsal_runner/bucket_proposer.py (develop a5ff112b).
    Copied, not imported, so agent-worker tests do not depend on rehearsal-runner."""
    records = [a for a in getattr(coordinator, "attempts", []) if a.get("attempt") == attempt]
    if len(records) != 1 or records[0].get("audit_truncated") is not False:
        raise ValueError("tool events unavailable")
    events = tuple(dict(e) for e in coordinator.audit
                   if e.get("attempt") == attempt and e.get("tool") != "independent_verifier")
    if len(events) != records[0].get("audit_event_count"):
        raise ValueError("tool events incomplete")
    return events


class ResultView:
    """The T15 oracle reads ``.attempts``/``.audit``; this lets it read a result dict too."""

    def __init__(self, result):
        self.attempts, self.audit = result["attempts"], result["audit"]


def install_sdk(monkeypatch, items):
    """The real ``AsyncOpenAI`` over a mock HTTP transport (same as test_openai.install_sdk)."""
    import httpx2
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    from bravo_agent import openai_provider

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        item = items[min(len(requests) - 1, len(items) - 1)]
        return httpx2.Response(200, json={
            "id": f"resp_{len(requests)}", "object": "response", "created_at": 1, "model": "fixture-model",
            "status": "completed", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"id": "fc_fixture", "type": "function_call", "status": "completed",
                        "call_id": "call_fixture", **item}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}})

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)))
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", client)
    monkeypatch.setattr(openai_provider.os, "environ", {"OPENAI_API_KEY": "model-fixture"})
    return requests


def call(name, arguments):
    return {"name": name, "arguments": json.dumps(arguments)}


def receipt(coordinator, verdict):
    values = {"run_id": coordinator.bundle.run_id, "source_sha": coordinator.bundle.source_sha,
              "proposal_digest": coordinator.result()["proposal_digest"], "attempt": coordinator.attempt,
              "verdict": verdict, "effects_known": True, "evidence_digest": "e" * 64}
    if verdict == "FAIL":
        values["failure"] = coordinator.bundle.model_copy(update={
            "summary": "Independent validator reports a remaining destination failure",
            "evidence_digest": "e" * 64})
    return VerificationReceipt(**values)


@pytest.fixture
def blocked_run(context, monkeypatch):
    """Two real attempts over the OpenAI path: attempt 1 tries a bucket delete (blocked by the
    tool layer), reads, proposes and fails verification; attempt 2 proposes and passes."""
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    install_sdk(monkeypatch, [call("rehearsal_exec", BUCKET_DELETE),
                              call("repo_read", {"path": "src/config.py"}),
                              call("plan_propose", proposal)])
    assert asyncio.run(coordinator.propose(OpenAIProvider("fixture-model")))["state"] == "PROPOSAL_READY"
    assert coordinator.verify(receipt(coordinator, "FAIL")) == "RETRY_ALLOWED"
    install_sdk(monkeypatch, [call("plan_propose", proposal)])
    assert asyncio.run(coordinator.propose(OpenAIProvider("fixture-model")))["state"] == "PROPOSAL_READY"
    assert coordinator.verify(receipt(coordinator, "PASS")) == "AWAITING_APPROVAL"
    return coordinator


def test_matches_t15_on_a_real_run_with_a_blocked_bucket_delete(blocked_run):
    result = blocked_run.result()
    wire = json.loads(json.dumps(result))  # what the T38 path receives from the worker
    for attempt in (1, 2):
        expected = t15_attempt_tool_events(blocked_run, attempt)
        assert attempt_tool_events(blocked_run, attempt) == expected
        assert attempt_tool_events(result, attempt) == expected
        assert attempt_tool_events(wire, attempt, run_id="run-1") == expected
        assert t15_attempt_tool_events(ResultView(wire), attempt) == expected
    first = attempt_tool_events(wire, 1)
    assert first[0] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
                        "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}
    assert [e["tool"] for e in first] == ["rehearsal.exec", "repo.read", "plan.propose", "model"]
    assert first[-1]["reason"] == "PROPOSAL_COLLECTED"
    assert all(e["tool"] != "independent_verifier" for e in first)
    assert any(e["tool"] == "independent_verifier" and e["attempt"] == 1 for e in result["audit"])
    assert [e["tool"] for e in attempt_tool_events(wire, 2)] == ["plan.propose", "model"]
    assert "private-bucket" not in json.dumps(result)


def test_returns_detached_copies(blocked_run):
    events = attempt_tool_events(blocked_run, 1)
    events[0]["category"] = "changed"
    assert blocked_run.audit[0]["category"] == "bucket_delete"


def test_matches_t15_on_a_stopped_attempt(context, monkeypatch):
    bundle, snapshot, _, _ = context
    coordinator = Coordinator(bundle, snapshot)
    install_sdk(monkeypatch, [call("rehearsal_exec", BUCKET_DELETE)])  # never proposes: loop limit
    assert asyncio.run(coordinator.propose(OpenAIProvider("fixture-model")))["state"] == "NEEDS_HUMAN"
    events = attempt_tool_events(coordinator, 1)
    assert events == t15_attempt_tool_events(coordinator, 1)
    assert events[-1] == {"run_id": "run-1", "attempt": 1, "tool": "model", "outcome": "STOPPED",
                          "reason": "MODEL_OR_POLICY_FAILURE"}
    assert {e["category"] for e in events[:-1]} == {"bucket_delete"}


class FloodProvider:
    async def propose(self, bundle, tools):
        for _ in range(130):
            try:
                tools.dispatch("mcp__bravo__rehearsal_exec", BUCKET_DELETE)
            except PolicyDenied:
                pass


def test_truncated_attempt_is_refused_like_t15(context):
    bundle, snapshot, _, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(FloodProvider()))
    record = coordinator.attempts[0]
    assert record["audit_truncated"] is True and record["audit_event_count"] == 131
    with pytest.raises(ValueError):
        t15_attempt_tool_events(coordinator, 1)
    with pytest.raises(ToolEventsUnavailable):
        attempt_tool_events(coordinator, 1)
    assert tool_events_projection(coordinator, 1) == {"tool_events_status": "UNAVAILABLE"}


def edited(coordinator, change):
    result = json.loads(json.dumps(coordinator.result()))
    change(result)
    return result


@pytest.mark.parametrize("change", [
    pytest.param(lambda r: r["audit"].pop(1), id="event-missing"),
    pytest.param(lambda r: r["audit"].insert(1, dict(r["audit"][1])), id="event-extra"),
    pytest.param(lambda r: r["attempts"].pop(0), id="record-missing"),
    pytest.param(lambda r: r["attempts"].append(dict(r["attempts"][0])), id="record-duplicate"),
    pytest.param(lambda r: r["attempts"][0].update(audit_truncated=None), id="truncated-unknown"),
    pytest.param(lambda r: r["attempts"][0].pop("audit_event_count"), id="count-missing"),
])
def test_incomplete_records_are_refused_by_both(blocked_run, change):
    result = edited(blocked_run, change)
    with pytest.raises(ValueError):
        t15_attempt_tool_events(ResultView(result), 1)
    with pytest.raises(ToolEventsUnavailable):
        attempt_tool_events(result, 1)


@pytest.mark.parametrize("change", [
    pytest.param(lambda r: r["audit"][0].update(run_id="run-2"), id="mixed-run"),
    pytest.param(lambda r: r["audit"][0].update(attempt=True), id="bool-attempt"),
    pytest.param(lambda r: r["audit"][0].update(category={"raw": "aws s3 rb"}), id="nested-value"),
    pytest.param(lambda r: r["audit"][0].update(category="aws s3 rb\nprivate"), id="control-char"),
    pytest.param(lambda r: r["audit"][0].update(detail=1.5), id="float"),
    pytest.param(lambda r: r["audit"][0].update(detail=None), id="null"),
    pytest.param(lambda r: r["audit"][0].pop("reason"), id="required-missing"),
])
def test_stricter_than_t15_unbound_or_malformed_events_are_refused(blocked_run, change):
    # T15 also refuses these before signing (collect_tool_events); here they never leave T11.
    with pytest.raises(ToolEventsUnavailable):
        attempt_tool_events(edited(blocked_run, change), 1)


def test_result_of_another_run_or_bad_input_is_refused(blocked_run):
    with pytest.raises(ToolEventsUnavailable):
        attempt_tool_events(blocked_run, 1, run_id="run-2")
    for attempt in (0, 3, True, "1"):
        with pytest.raises(ToolEventsUnavailable):
            attempt_tool_events(blocked_run, attempt)
    for source in (None, [], {"run_id": "run-1"}, {"run_id": "run-1", "attempts": [1], "audit": []}):
        with pytest.raises(ToolEventsUnavailable):
            attempt_tool_events(source, 1)


def test_after_pr_372_coordinator_stop_events_count_toward_the_attempt(blocked_run):
    # PR #372's ``_stop`` appends {"tool": "coordinator", "outcome": "STOPPED"} and adds one to the
    # attempt's audit_event_count. Simulated on the result dict so this test holds before and after.
    def stop(result):
        result["audit"].append({"run_id": "run-1", "attempt": 2, "tool": "coordinator",
                                "outcome": "STOPPED", "reason": "STATE_CHECKPOINT_FAILED"})
        result["attempts"][1]["audit_event_count"] += 1
    result = edited(blocked_run, stop)
    events = attempt_tool_events(result, 2)
    assert events == t15_attempt_tool_events(ResultView(result), 2)
    assert [e["tool"] for e in events] == ["plan.propose", "model", "coordinator"]

    # PR #372's restore of an in-flight attempt: a record with audit_truncated=True, count 1.
    def restarted(result):
        result["attempts"].append({**result["attempts"][1], "attempt": 3, "audit_event_count": 1,
                                   "audit_truncated": True})
        result["audit"].append({"run_id": "run-1", "attempt": 3, "tool": "coordinator",
                                "outcome": "STOPPED", "reason": "RESTART_OUTCOME_UNKNOWN"})
    result = edited(blocked_run, restarted)
    with pytest.raises(ValueError):
        t15_attempt_tool_events(ResultView(result), 3)
    assert tool_events_projection(result, 3) == {"tool_events_status": "UNAVAILABLE"}


def test_subscription_first_response_diagnostic_makes_the_attempt_unavailable(context, monkeypatch):
    # Known gap, kept visible: the chatgpt-plan path (the one T38 runs) records FIRST_RESPONSE_SHAPE
    # with nested counters, which is outside the str/int/bool contract T15 also enforces. The whole
    # attempt is refused instead of dropping that event. Flattening that diagnostic is a follow-up.
    from test_subscription_provider import completed, install_stream

    bundle, snapshot, proposal, _ = context
    install_stream(monkeypatch, [[completed("repo_read", {"path": "src/config.py"})],
                                 [completed("plan_propose", proposal)]])
    coordinator = Coordinator(bundle, snapshot)
    result = asyncio.run(coordinator.propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "PROPOSAL_READY"
    shape, = [e for e in result["audit"] if e["reason"] == "FIRST_RESPONSE_SHAPE"]
    assert isinstance(shape["terminal_items"], dict)
    assert len(t15_attempt_tool_events(coordinator, 1)) == result["attempts"][0]["audit_event_count"]
    assert tool_events_projection(coordinator, 1) == {"tool_events_status": "UNAVAILABLE"}


def test_projection_carries_the_whole_list_or_nothing(blocked_run):
    wire = json.loads(json.dumps(blocked_run.result()))
    projected = tool_events_projection(wire, 1, run_id="run-1")
    assert projected == {"tool_events_status": "COMPLETE", "tool_events": list(attempt_tool_events(wire, 1))}
    assert tool_events_projection(wire, 1, run_id="run-2") == {"tool_events_status": "UNAVAILABLE"}


def event(**changes):
    return {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
            "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete", **changes}


INVALID_EVENTS = [
    pytest.param(event(tool="independent_verifier"), id="verifier"),
    pytest.param(event(tool="Rehearsal Exec"), id="tool-pattern"),
    pytest.param(event(outcome="denied"), id="outcome-pattern"),
    pytest.param(event(reason=7), id="reason-type"),
    pytest.param(event(category="x" * 257), id="long-text"),
    pytest.param(event(category="버킷"), id="non-ascii"),
    pytest.param(event(items=2**53), id="unsafe-int"),
    pytest.param(event(items=[1]), id="list"),
    pytest.param(event(**{"Category": "x"}), id="field-name"),
    pytest.param(event(**{f"extra_{i}": i for i in range(11)}), id="too-many-fields"),
    pytest.param({"run_id": "run-1", "attempt": 1, "tool": "model", "outcome": "STOPPED"}, id="no-reason"),
]


@pytest.mark.parametrize("value", INVALID_EVENTS)
def test_validator_rejects_out_of_contract_events(value):
    with pytest.raises(ToolEventsUnavailable):
        validate_tool_events([value], "run-1", 1)


def test_validator_binds_run_and_attempt_and_bounds_size():
    assert validate_tool_events((), "run-1", 1) == ()
    assert validate_tool_events([event(items=3, allowed=False)], "run-1", 1) == (event(items=3, allowed=False),)
    for run_id, attempt in (("run-2", 1), ("run-1", 2), ("bad run", 1), ("run-1", True)):
        with pytest.raises(ToolEventsUnavailable):
            validate_tool_events([event()], run_id, attempt)
    assert len(validate_tool_events([event()] * MAX_TOOL_EVENTS, "run-1", 1)) == MAX_TOOL_EVENTS
    with pytest.raises(ToolEventsUnavailable):
        validate_tool_events([event()] * (MAX_TOOL_EVENTS + 1), "run-1", 1)
    with pytest.raises(ToolEventsUnavailable):
        validate_tool_events({"0": event()}, "run-1", 1)


def test_size_bound_applies_to_extraction(blocked_run):
    def flood(result):
        result["audit"] = [e for e in result["audit"] if e["attempt"] != 2]
        result["audit"] += [event(attempt=2, tool="repo.read")] * (MAX_TOOL_EVENTS + 1)
        result["attempts"][1]["audit_event_count"] = MAX_TOOL_EVENTS + 1
    with pytest.raises(ToolEventsUnavailable):
        attempt_tool_events(edited(blocked_run, flood), 2)


def test_json_schema_agrees_with_the_validator(blocked_run):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(TOOL_EVENTS_SCHEMA)
    schema = jsonschema.Draft202012Validator(TOOL_EVENTS_SCHEMA)
    real = list(attempt_tool_events(blocked_run, 1))
    assert schema.is_valid(real)
    for value in INVALID_EVENTS:
        assert not schema.is_valid([value.values[0]]), value.id
    assert not schema.is_valid([event()] * (MAX_TOOL_EVENTS + 1))


def test_module_imports_no_llm_client():
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"re", "collections"}


def test_does_not_mutate_its_source(blocked_run):
    result = blocked_run.result()
    before = copy.deepcopy(result)
    attempt_tool_events(result, 1)
    tool_events_projection(result, 2)
    assert result == before
