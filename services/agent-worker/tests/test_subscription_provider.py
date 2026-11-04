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
    calls = install_stream(monkeypatch, [[completed("repo_read", {"path": "src/config.py"})],
                                         [completed("plan_propose", proposal)]])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=True)))
    assert result["state"] == "PROPOSAL_READY" and len(calls) == 2
    assert result["attempts"][0]["model_usage"] == {
        "provider": "chatgpt_plan", "model": "fixture-model", "requests": 2, "total_tokens": 300}
    for call in calls:
        assert call["stream"] is True and call["store"] is False
        assert not {"max_output_tokens", "previous_response_id", "conversation", "metadata"} & call.keys()
        assert call["tools"][0]["type"] == "namespace" and call["tools"][0]["name"] == "bravo"
    shapes = [event for event in result["audit"] if event["reason"] == "FIRST_RESPONSE_SHAPE"]
    assert len(shapes) == 1 and shapes[0]["function_call_count"] == 1 and shapes[0]["bravo_namespace_matches"] is True
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
                     "bravo_namespace_matches": match}
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None and len(requests) == 1
    assert stopped["reason"] == "MODEL_OR_POLICY_FAILURE"
    assert "private-" not in json.dumps(result)
