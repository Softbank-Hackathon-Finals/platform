import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from bravo_agent import openai_provider
from bravo_agent.coordinator import Coordinator
from bravo_agent.openai_provider import OpenAIProvider


def install_stream(monkeypatch, streams, status=200):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer subscription-fixture"
        assert str(request.url) == "https://api.openai.com/v1/responses"
        events = streams[min(len(requests) - 1, len(streams) - 1)]
        if status != 200:
            return httpx2.Response(status, json=events)
        content = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(**kwargs, http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)))
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", client)
    monkeypatch.setattr(openai_provider.os, "environ", {"ACCESS_TOKEN": "subscription-fixture"})
    return requests


def completed(name, args, namespace="bravo"):
    return {"type": "response.completed", "sequence_number": 1, "response": {
        "id": "resp_fixture", "object": "response", "created_at": 1, "model": "fixture-model",
        "status": "completed", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "output": [{"id": "fc_fixture", "type": "function_call", "status": "completed", "namespace": namespace,
                    "name": name, "arguments": json.dumps(args), "call_id": "call_fixture"}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }}


def test_real_sdk_subscription_stream_namespaces_tools_and_collects_proposal(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    first = completed("repo_read", {"path": "src/config.py"})
    item = first["response"]["output"][0]
    added = {"type": "response.output_item.added", "output_index": 0, "sequence_number": 0,
             "item": dict(item, arguments="", status="in_progress")}
    done = dict(added, type="response.output_item.done", sequence_number=1, item=item)
    first["sequence_number"] = 2
    calls = install_stream(monkeypatch, [[added, done, first], [completed("plan_propose", proposal)]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "PROPOSAL_READY" and len(calls) == 2
    assert result["attempts"][0]["model_usage"] == {
        "provider": "chatgpt_plan", "model": "fixture-model", "requests": 2, "total_tokens": 300}
    for call in calls:
        assert call["stream"] is True and call["store"] is False
        assert not {"max_output_tokens", "previous_response_id", "conversation", "metadata"} & call.keys()
        assert call["tools"][0]["type"] == "namespace" and call["tools"][0]["name"] == "bravo"
    assert calls[0]["tool_choice"] == "required"
    assert [t["name"] for t in calls[0]["tools"][0]["tools"]] == ["repo_read"]
    assert calls[1]["tool_choice"] == "auto"
    assert {t["name"] for t in calls[1]["tools"][0]["tools"]} == {"repo_read", "plan_propose"}
    shapes = [event for event in result["audit"] if event["reason"] == "FIRST_RESPONSE_SHAPE"]
    assert len(shapes) == 1 and shapes[0]["function_call_count"] == 1 and shapes[0]["bravo_namespace_matches"] is True
    assert shapes[0]["stream_added"] == shapes[0]["stream_done"] == shapes[0]["terminal_items"] == {"bravo_repo_read": 1}
    assert calls[1]["input"][-1]["type"] == "function_call_output"
    assert "subscription-fixture" not in str(result)


@pytest.mark.parametrize("failure", ["failed", "incomplete", "missing", "wrong_namespace"])
def test_stream_deltas_never_count_as_success(context, monkeypatch, failure):
    bundle, snapshot, proposal, _ = context
    delta = {"type": "response.output_text.delta", "sequence_number": 0,
             "delta": "private-provider-message", "content_index": 0, "output_index": 0,
             "item_id": "item_fixture", "logprobs": []}
    terminal = completed("plan_propose", proposal)
    if failure == "wrong_namespace":
        terminal["response"]["output"][0]["namespace"] = "shell"
    else:
        terminal["type"] = "response." + failure
        terminal["response"]["status"] = failure
        terminal["response"]["error"] = {"code": "subscription_sharing_usage_limit_exceeded", "message": "private-detail"}
    events = [delta] if failure == "missing" else [delta, terminal]
    calls = install_stream(monkeypatch, [events])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None and len(calls) == 1
    assert "private-" not in str(result)
    if failure == "failed":
        assert "subscription_sharing_usage_limit_exceeded" in str(result["audit"])


@pytest.mark.parametrize("body", [{"error": {"code": "subscription_sharing_user_not_eligible", "message": "private-detail"}},
                                  {"detail": "private-detail"}])
def test_admission_failure_is_bounded_and_never_billed_through_api_fallback(context, monkeypatch, body):
    bundle, snapshot, _, _ = context
    calls = install_stream(monkeypatch, [body], status=403)
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert len(calls) == 1 and result["state"] == "NEEDS_HUMAN" and result["proposal"] is None
    assert "private-detail" not in str(result) and "403" in str(result["audit"])


@pytest.mark.parametrize(("namespaces", "match"), [
    ([], None), (["private-wrong-namespace"], False), ([None], False),
    (["bravo", "bravo"], True), (["bravo", "private-wrong-namespace"], False),
])
def test_first_subscription_response_shape_is_private_and_fails_closed(context, monkeypatch, namespaces, match):
    bundle, snapshot, _, _ = context
    terminal = completed("private-function", {"private-key": "private-value"})
    output = terminal["response"]["output"].pop()
    terminal["response"]["output"] = [dict(output, namespace=ns) for ns in namespaces]
    if not namespaces:
        terminal["response"]["output"].append({
            "id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "private-response-text", "annotations": []}],
        })
    requests = install_stream(monkeypatch, [[terminal]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    shape, stopped = result["audit"]
    assert shape == {"run_id": bundle.run_id, "attempt": 1, "tool": "model", "outcome": "OBSERVED",
                     "reason": "FIRST_RESPONSE_SHAPE", "function_call_count": len(namespaces),
                     "bravo_namespace_matches": match, "stream_added": {}, "stream_done": {},
                     "terminal_output_missing": False,
                     "terminal_items": {"other_function": len(namespaces)} if namespaces else {"message": 1}}
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None and len(requests) == 1
    assert stopped["reason"] == "MODEL_OR_POLICY_FAILURE"
    assert "private-" not in json.dumps(result)

@pytest.mark.parametrize("bad_read", [False, True])
def test_proposal_before_successful_read_is_rejected(context, monkeypatch, bad_read):
    bundle, snapshot, proposal, _ = context
    streams = [[completed("repo_read", {"path": "../outside"})]] if bad_read else []
    streams.append([completed("plan_propose", proposal)])
    calls = install_stream(monkeypatch, streams)
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert len(calls) == len(streams) and all(c["tool_choice"] == "required" for c in calls)
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None


def test_subscription_can_stop_after_a_read_without_proposing(context, monkeypatch):
    bundle, snapshot, _, _ = context
    no_call = completed("plan_propose", {})
    no_call["response"]["output"] = []
    calls = install_stream(monkeypatch, [[completed("repo_read", {"path": "src/config.py"})], [no_call]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert [c["tool_choice"] for c in calls] == ["required", "auto"]
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None


@pytest.mark.parametrize("terminal_shape", ["empty", "omitted", "refusal"])
def test_done_call_is_observed_but_never_dispatched_without_terminal_call(context, monkeypatch, terminal_shape):
    bundle, snapshot, _, _ = context
    terminal = completed("repo_read", {"path": "src/config.py"})
    item = terminal["response"]["output"].pop()
    item["arguments"] = '{"path":"private-provider-path"}'
    events = [{"type": "response.output_item.done", "output_index": 0, "sequence_number": 0, "item": item}]
    if terminal_shape == "omitted":
        del terminal["response"]["output"]
    elif terminal_shape == "refusal":
        terminal["response"]["output"] = [{
            "type": "message", "id": "private-id", "status": "completed", "role": "assistant",
            "content": [{"type": "refusal", "refusal": "private-provider-reason"}],
        }]
    requests = install_stream(monkeypatch, [[*events, terminal]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    audit = result["audit"]
    shape, stop = audit
    assert shape["reason"] == "FIRST_RESPONSE_SHAPE"
    assert shape["stream_done"] == {"bravo_repo_read": 1}
    assert shape["terminal_items"] == ({"refusal": 1} if terminal_shape == "refusal" else {})
    assert shape["terminal_output_missing"] is (terminal_shape == "omitted")
    assert len(requests) == 1 and result["state"] == "NEEDS_HUMAN" and result["proposal"] is None
    assert stop["reason"] == "MODEL_OR_POLICY_FAILURE" and "private-" not in json.dumps(result)


def streamed_call(name, args):
    final = completed(name, args)
    item = final["response"]["output"].pop()
    created = dict(final, type="response.created", sequence_number=0,
                   response=dict(final["response"], status="in_progress", usage=None))
    added = {"type": "response.output_item.added", "output_index": 0, "sequence_number": 1,
             "item": dict(item, arguments="", status="in_progress")}
    done = dict(added, type="response.output_item.done", sequence_number=2, item=item)
    return [created, added, done, dict(final, sequence_number=3)]


@pytest.mark.parametrize(("kind", "position", "label", "phase", "extra", "fields"), [
    ("response.metadata", 0, "response.metadata", "before_response", {"metadata": {"id": "private-value"}}, ["metadata"]),
    ("codex.rate_limits", 1, "codex.rate_limits", "after_created", {"rate_limits": {"id": "private-value"}}, ["rate_limits"]),
    ("response.refusal.delta", 2, "response.refusal.delta", "after_added", {"delta": "private-value"}, ["delta"]),
    ("private-event", 3, "other", "after_done", {"private-field": "private-value"}, []),
])
def test_first_rejected_event_reports_protocol_shape_without_admitting_it(
        context, monkeypatch, kind, position, label, phase, extra, fields):
    bundle, snapshot, _proposal, _ = context
    events = streamed_call("repo_read", {"path": "src/config.py"})
    events.insert(position, {"type": kind, "sequence_number": 0, **extra})
    for index, event in enumerate(events):
        event["sequence_number"] = index
    requests = install_stream(monkeypatch, [events])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    shape, stop = result["audit"]
    assert shape["rejected_event"] == {"kind": label, "phase": phase, "fields": fields, "other_fields": not fields}
    assert shape["stream_resolution"] == "unrecognized_event" and shape["function_call_count"] == 0
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None and len(requests) == 1
    assert stop["reason"] == "MODEL_OR_POLICY_FAILURE" and "private-" not in json.dumps(result)


def test_completed_streamed_call_with_empty_terminal_runs_under_normal_tool_policy(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    read = streamed_call("repo_read", {"path": "src/config.py"})
    args = read[2]["item"]["arguments"]
    read[2]["sequence_number"], read[3]["sequence_number"] = 4, 5
    read[2:2] = [
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_fixture",
         "sequence_number": 2, "delta": args},
        {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc_fixture",
         "sequence_number": 3, "arguments": args},
    ]
    requests = install_stream(monkeypatch, [read, streamed_call("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "PROPOSAL_READY" and len(requests) == 2
    shape, = [e for e in result["audit"] if e["reason"] == "FIRST_RESPONSE_SHAPE"]
    assert shape["stream_resolution"] == "matched_finalized_call"
    assert shape["terminal_items"] == {} and shape["function_call_count"] == 1
    assert shape["stream_done"] == {"bravo_repo_read": 1} and shape["bravo_namespace_matches"] is True
    assert requests[1]["input"][-1]["call_id"] == "call_fixture"


@pytest.mark.parametrize("case", ["no_done", "no_terminal", "other_response", "other_index", "other_call",
                                  "other_item", "wrong_namespace", "partial", "duplicate", "extra_message",
                                  "wrong_name", "args_mismatch", "incomplete_status", "no_usage", "early_proposal", "bad_path"])
def test_unbound_or_denied_stream_output_cannot_submit(context, monkeypatch, case):
    bundle, snapshot, proposal, _ = context
    events = streamed_call("plan_propose", proposal) if case == "early_proposal" else streamed_call(
        "repo_read", {"path": "../private-path" if case == "bad_path" else "src/config.py"})
    created, added, done, final = events
    if case == "no_done":
        events.remove(done)
    elif case == "no_terminal":
        events.remove(final)
    elif case == "other_response":
        created["response"]["id"] = "different"
    elif case == "other_index":
        done["output_index"] = 1
    elif case in ("other_call", "other_item", "wrong_namespace", "wrong_name", "partial"):
        field = {"other_call": "call_id", "other_item": "id", "wrong_namespace": "namespace",
                 "wrong_name": "name", "partial": "status"}[case]
        done["item"][field] = "in_progress" if case == "partial" else "different"
    elif case == "duplicate":
        events.insert(-1, done)
    elif case == "extra_message":
        events.insert(-1, dict(done, output_index=1, item={
            "id": "msg", "type": "message", "status": "completed", "role": "assistant", "content": []}))
    elif case == "args_mismatch":
        events.insert(2, {"type": "response.function_call_arguments.done", "item_id": "fc_fixture", "output_index": 0,
                          "sequence_number": 2, "arguments": "{}"})
    elif case == "incomplete_status":
        final["response"]["status"] = "incomplete"
    elif case == "no_usage":
        final["response"]["usage"] = None
    requests = install_stream(monkeypatch, [events, [completed("plan_propose", proposal)]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None
    assert len(requests) == (2 if case == "bad_path" else 1)
    assert all(r["tool_choice"] == "required" for r in requests)
