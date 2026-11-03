"""OpenAI Responses tool loop; every requested action goes through shared policy."""

import asyncio
import copy
import json
import os
import re

from openai import APIStatusError, AsyncOpenAI

from .contracts import FailureBundle, Proposal
from .runtime import RUNTIME_KEYS
from .tools import SYSTEM, PolicyDenied, Tools

FUNCTIONS = {"repo_read": "mcp__bravo__repo_read", "plan_propose": "mcp__bravo__plan_propose"}


def strict_schema(schema: dict) -> dict:
    schema = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


def function_tools() -> list[dict]:
    return [
        {"type": "function", "name": "repo_read", "strict": True,
         "description": "Read an approved application snapshot file.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False}},
        {"type": "function", "name": "plan_propose", "strict": True,
         "description": "Submit a bounded correction; does not apply or verify it.",
         "parameters": strict_schema(Proposal.model_json_schema())},
    ]


class OpenAIProvider:
    def __init__(self, model: str, *, subscription: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model):
            raise ValueError("invalid model identifier")
        self.model = model
        self.subscription = subscription

    def record_subscription_error(self, tools, code=None, status=None):
        known = {"subscription_sharing_user_not_eligible", "subscription_sharing_usage_limit_exceeded",
                 "subscription_sharing_usage_unavailable", "subscription_sharing_unsupported_capability",
                 "subscription_sharing_route_not_supported", "subscription_sharing_invalid_user",
                 "subscription_sharing_user_unavailable", "chatpass_v2_scope_not_authorized",
                 "chatpass_v2_invalid_authorization_context"}
        tools.record("provider", "STOPPED", "SUBSCRIPTION_REQUEST_FAILED", {
            "category": code if code in known else "unknown", "http_status": status})

    async def response(self, client, request, tools):
        if not self.subscription:
            return await client.responses.create(**request)
        try:
            stream = await client.responses.create(**request, stream=True)
            size = 0
            async with stream:
                async for event in stream:
                    size += len(event.model_dump_json().encode())
                    if size > 2097152:
                        raise RuntimeError("subscription stream exceeded bounded response size")
                    if event.type == "response.failed":
                        error = event.response.error
                        self.record_subscription_error(tools, getattr(error, "code", None))
                        raise RuntimeError("subscription stream failed")
                    if event.type in {"response.incomplete", "error"}:
                        raise RuntimeError("subscription stream incomplete")
                    if event.type == "response.completed":
                        return event.response
            raise RuntimeError("subscription stream ended without completion")
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            detail = body.get("error", body)
            code = detail.get("code") if isinstance(detail, dict) else None
            self.record_subscription_error(tools, code, error.status_code)
            raise RuntimeError("subscription request rejected") from None

    async def propose(self, bundle: FailureBundle, tools: Tools) -> None:
        credential = "ACCESS_TOKEN" if self.subscription else "OPENAI_API_KEY"
        other_credentials = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ACCESS_TOKEN"} - {credential}
        if (set(os.environ) - RUNTIME_KEYS or other_credentials & set(os.environ)
                or not os.environ.get(credential)):
            raise RuntimeError("worker process environment must be explicitly isolated")
        history = [{"role": "user", "content": json.dumps({
            "failure": bundle.model_dump(), "snapshot": tools.snapshot.manifest.model_dump(),
        }, sort_keys=True)}]
        definitions = function_tools()
        if self.subscription:
            definitions = [{"type": "namespace", "name": "bravo",
                            "description": "Approved snapshot reads and proposal submission only.",
                            "tools": definitions}]
        tokens = 0
        async with asyncio.timeout(180):
            async with AsyncOpenAI(api_key=os.environ[credential],
                                   base_url="https://api.openai.com/v1",
                                   max_retries=0, timeout=60) as client:
                for index in range(8):
                    request = {"model": self.model, "instructions": SYSTEM,
                               "input": history, "tools": definitions, "tool_choice": "auto",
                               "parallel_tool_calls": False, "store": False,
                               "include": ["reasoning.encrypted_content"]}
                    if not self.subscription:
                        request["max_output_tokens"] = min(4096, 32000 - tokens)
                    if tokens >= 32000 or len(json.dumps(request).encode()) > 131072:
                        raise RuntimeError("model input or token budget reached")
                    response = await self.response(client, request, tools)
                    if response.status != "completed" or response.usage is None:
                        raise RuntimeError("incomplete model response or unknown usage")
                    if type(response.usage.total_tokens) is not int or response.usage.total_tokens < 0:
                        raise RuntimeError("invalid model usage")
                    tokens += response.usage.total_tokens
                    tools.usage = {"provider": "chatgpt_plan" if self.subscription else "openai", "model": self.model,
                                   "requests": index + 1, "total_tokens": tokens}
                    if tokens > 32000:
                        raise RuntimeError("model usage limit reached")
                    outputs = [item.model_dump(exclude_none=True) for item in response.output]
                    calls = [item for item in outputs if item["type"] == "function_call"]
                    if len(calls) != 1:
                        raise RuntimeError("expected one allowed function call")
                    history.extend(outputs)
                    item = calls[0]
                    if self.subscription and item.get("namespace") != "bravo":
                        raise RuntimeError("unexpected subscription tool namespace")
                    name = FUNCTIONS.get(item["name"], "unlisted")
                    try:
                        if len(item["arguments"]) > 140000:
                            raise ValueError("arguments too large")
                        arguments = json.loads(item["arguments"])
                        result = tools.dispatch(name, arguments)
                    except (ValueError, PolicyDenied):
                        result = {"error": "POLICY_DENIED"}
                        if name in FUNCTIONS.values():
                            tools.record(name, "DENIED", "INVALID_FUNCTION_ARGUMENTS")
                    if tools.proposal is not None:
                        tools.record("provider", "COMPLETED", "PROPOSAL_COLLECTED")
                        return
                    history.append({"type": "function_call_output", "call_id": item["call_id"],
                                    "output": json.dumps(result)})
                raise RuntimeError("model call limit reached")
