import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from bravo_agent.coordinator import Coordinator
from bravo_agent.openai_provider import OpenAIProvider, function_tools
from bravo_agent.runtime import isolated_environment


class Output:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **kwargs):
        return self.data


def response(name, arguments, *, status="completed", tokens=100):
    return SimpleNamespace(status=status, usage=SimpleNamespace(total_tokens=tokens),
                           output=[Output(type="function_call", name=name,
                                          arguments=json.dumps(arguments), call_id="call-fixture")])


def install_client(monkeypatch, replies):
    from bravo_agent import openai_provider

    calls, options = [], {}

    class Client:
        def __init__(self, **kwargs):
            options.update(kwargs)
            self.responses = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create(self, **request):
            calls.append(copy.deepcopy(request))
            return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", Client)
    monkeypatch.setattr(openai_provider.os, "environ", {"OPENAI_API_KEY": "model-fixture"})
    return calls, options


def test_openai_uses_two_strict_functions_and_collects_only_proposal(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    calls, options = install_client(monkeypatch, [
        response("repo_read", {"path": "src/config.py"}), response("plan_propose", proposal),
    ])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY"
    assert len(calls) == 2
    assert options["max_retries"] == 0
    assert options["base_url"] == "https://api.openai.com/v1"
    assert all(call["store"] is False and call["parallel_tool_calls"] is False for call in calls)
    assert {tool["name"] for tool in calls[0]["tools"]} == {"repo_read", "plan_propose", "policy_evaluate"}  # inventory_describe only with a T16 summary
    assert all(tool["type"] == "function" and tool["strict"] for tool in calls[0]["tools"])
    assert calls[1]["input"][-1]["type"] == "function_call_output"
    first_input = json.loads(calls[0]["input"][0]["content"])
    assert set(first_input) == {"task", "failure", "attempt", "snapshot"} and first_input["task"] == first_input["failure"]
    assert result["attempts"][0]["model_usage"]["total_tokens"] == 200
    assert "model-fixture" not in str(result)


def test_strict_schema_has_no_implicit_optional_fields():
    def visit(node):
        if isinstance(node, dict):
            assert "default" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == node["properties"].keys()
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    for tool in function_tools():
        visit(tool["parameters"])


def test_openai_unknown_tool_is_denied_by_same_handler(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    calls, _ = install_client(monkeypatch, [
        response("shell", {"command": "do-not-record-input"}), response("plan_propose", proposal),
    ])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY"
    assert result["audit"][0]["reason"] == "TOOL_NOT_ALLOWED"
    assert "do-not-record-input" not in str(result)
    assert "POLICY_DENIED" in calls[1]["input"][-1]["output"]


def test_openai_loop_is_bounded(context, monkeypatch):
    bundle, snapshot, _, _ = context
    calls, _ = install_client(monkeypatch, [response("repo_read", {"path": "src/config.py"})])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert len(calls) == 8
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None


@pytest.mark.parametrize("status,tokens", [("incomplete", 100), ("completed", 32001),
                                          ("completed", -1)])
def test_openai_incomplete_or_over_budget_response_is_not_accepted(context, monkeypatch, status, tokens):
    bundle, snapshot, proposal, _ = context
    calls, _ = install_client(monkeypatch, [response("plan_propose", proposal,
                                                    status=status, tokens=tokens)])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None
    assert len(calls) == 1


def test_openai_default_environment_excludes_other_provider_and_infra_credentials(tmp_path):
    result = isolated_environment({"OPENAI_API_KEY": "openai-fixture",
                                   "ANTHROPIC_API_KEY": "other-provider-fixture",
                                   "AWS_SECRET_ACCESS_KEY": "cloud-fixture",
                                   "OPENAI_BASE_URL": "https://unapproved.example"}, tmp_path)
    assert result["OPENAI_API_KEY"] == "openai-fixture"
    assert not {"ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "OPENAI_BASE_URL"} & result.keys()


@pytest.mark.parametrize("ambient", ["GH_TOKEN", "ANTHROPIC_API_KEY"])
def test_library_rejects_ambient_credentials_before_model_call(context, monkeypatch, ambient):
    from bravo_agent import openai_provider

    bundle, snapshot, proposal, _ = context
    calls, _ = install_client(monkeypatch, [response("plan_propose", proposal)])
    openai_provider.os.environ[ambient] = "must-not-be-inherited"
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert not calls


def test_real_openai_sdk_serializes_and_parses_without_network(context, monkeypatch):
    import httpx2
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    from bravo_agent import openai_provider

    bundle, snapshot, proposal, _ = context
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "resp_fixture", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "parallel_tool_calls": False,
            "tool_choice": "auto", "tools": requests[-1]["tools"],
            "output": [{"id": "fc_fixture", "type": "function_call", "status": "completed",
                        "name": "plan_propose", "arguments": json.dumps(proposal),
                        "call_id": "call_fixture"}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
        })

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handle)))

    monkeypatch.setattr(openai_provider, "AsyncOpenAI", client)
    monkeypatch.setattr(openai_provider.os, "environ", {"OPENAI_API_KEY": "model-fixture"})
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert len(requests) == 1
    assert result["state"] == "PROPOSAL_READY"
    assert result["attempts"][0]["model_usage"]["total_tokens"] == 150
