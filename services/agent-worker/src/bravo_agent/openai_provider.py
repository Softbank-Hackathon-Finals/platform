"""OpenAI Responses tool loop; every requested action goes through shared policy."""

import asyncio
import copy
import json
import os
import re
from collections import Counter

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


def output_kind(item) -> str:
    """Return a code-owned label; never retain response text, names, IDs or arguments."""
    kind = getattr(item, "type", None)
    if kind == "function_call":
        if getattr(item, "namespace", None) != "bravo":
            return "other_function"
        for name in FUNCTIONS:
            if getattr(item, "name", None) == name:
                return "bravo_" + name
        return "other_function"
    if kind == "message":
        content = getattr(item, "content", None)
        return "refusal" if isinstance(content, list) and any(getattr(p, "type", None) == "refusal" for p in content) else "message"
    return kind if kind in ("reasoning", "custom_tool_call", "tool_search_call") else "other"


class FinalizedStreamCall:
    """Keep one matched function item for a completed response with an explicit empty output."""
    def __init__(self):
        self.response_id = self.added = self.index = self.done = self.final_args = None
        self.problem = None

    def observe(self, event):
        if self.problem:
            return
        if event.type == "response.created":
            if self.response_id is not None or event.response.output:
                self.problem = "ambiguous_start"
            self.response_id = event.response.id
        elif event.type in ("response.in_progress", "response.queued"):
            if event.response.id != self.response_id or event.response.output:
                self.problem = "lifecycle_mismatch"
        elif event.type in ("response.output_item.added", "response.output_item.done"):
            item = getattr(event, "item", None)
            if (not self.response_id or output_kind(item) not in ("bravo_repo_read", "bravo_plan_propose")
                    or not isinstance(item.call_id, str) or not item.call_id
                    or type(event.output_index) is not int or event.output_index < 0):
                self.problem = "unbound_item"
            elif event.type == "response.output_item.added":
                if self.added is not None or item.status not in (None, "in_progress", "completed"):
                    self.problem = "ambiguous_item"
                self.added, self.index = item, event.output_index
            elif (self.added is None or self.done is not None or event.output_index != self.index
                  or item.status not in (None, "completed") or item.name != self.added.name
                  or item.call_id != self.added.call_id or (self.added.id and self.added.id != item.id)
                  or (self.added.arguments and self.added.arguments != item.arguments)
                  or (self.final_args is not None and self.final_args != item.arguments)):
                self.problem = "unmatched_done"
            else:
                self.done = item
        elif event.type in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            if (self.added is None or self.done is not None or event.output_index != self.index
                    or not self.added.id or event.item_id != self.added.id or self.final_args is not None):
                self.problem = "unbound_arguments"
            elif event.type == "response.function_call_arguments.done":
                self.final_args = event.arguments
        elif event.type != "response.completed":
            self.problem = "unrecognized_event"

    def resolved(self, response):
        if (self.problem or not self.response_id or response.id != self.response_id
                or response.status != "completed" or response.error or response.incomplete_details):
            return None, self.problem or "unbound_response"
        if self.done is None:
            return None, "no_finalized_call"
        return self.done, "matched_finalized_call"


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

    async def response(self, client, request, tools, stream_shape=None):
        if not self.subscription:
            return await client.responses.create(**request)
        try:
            stream = await client.responses.create(**request, stream=True)
            size = 0
            candidate = FinalizedStreamCall()
            async with stream:
                async for event in stream:
                    size += len(event.model_dump_json().encode())
                    if size > 2097152:
                        raise RuntimeError("subscription stream exceeded bounded response size")
                    candidate.observe(event)
                    if stream_shape is not None and event.type in ("response.output_item.added", "response.output_item.done"):
                        bucket = "stream_done" if event.type == "response.output_item.done" else "stream_added"
                        stream_shape[bucket][output_kind(getattr(event, "item", None))] += 1
                    if event.type == "response.failed":
                        error = event.response.error
                        self.record_subscription_error(tools, getattr(error, "code", None))
                        raise RuntimeError("subscription stream failed")
                    if event.type in {"response.incomplete", "error"}:
                        raise RuntimeError("subscription stream incomplete")
                    if event.type == "response.completed":
                        response = event.response
                        if stream_shape is not None:
                            stream_shape["terminal_output_missing"] = response.output is None
                            stream_shape["terminal_items"] = dict(Counter(output_kind(item) for item in response.output or []))
                        if response.output == []:
                            item, reason = candidate.resolved(response)
                            if stream_shape is not None:
                                stream_shape["stream_resolution"] = reason
                            if item is not None:
                                response = response.model_copy(update={"output": [item]})
                        return response
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
        needs_read = self.subscription
        tokens = 0
        async with asyncio.timeout(180):
            async with AsyncOpenAI(api_key=os.environ[credential],
                                   base_url="https://api.openai.com/v1",
                                   max_retries=0, timeout=60) as client:
                for index in range(8):
                    offered = definitions
                    if needs_read:
                        offered = [tool for tool in definitions if tool["name"] == "repo_read"]
                    if self.subscription:
                        offered = [{"type": "namespace", "name": "bravo", "tools": offered,
                                    "description": "Approved snapshot reads and proposal submission only."}]
                    request = {"model": self.model, "instructions": SYSTEM,
                               "input": history, "tools": offered, "tool_choice": "required" if needs_read else "auto",
                               "parallel_tool_calls": False, "store": False,
                               "include": ["reasoning.encrypted_content"]}
                    if not self.subscription:
                        request["max_output_tokens"] = min(4096, 32000 - tokens)
                    if tokens >= 32000 or len(json.dumps(request).encode()) > 131072:
                        raise RuntimeError("model input or token budget reached")
                    stream_shape = {"stream_added": Counter(), "stream_done": Counter()} if self.subscription and index == 0 else None
                    response = await self.response(client, request, tools, stream_shape)
                    if response.status != "completed" or response.usage is None:
                        raise RuntimeError("incomplete model response or unknown usage")
                    if type(response.usage.total_tokens) is not int or response.usage.total_tokens < 0:
                        raise RuntimeError("invalid model usage")
                    tokens += response.usage.total_tokens
                    tools.usage = {"provider": "chatgpt_plan" if self.subscription else "openai", "model": self.model,
                                   "requests": index + 1, "total_tokens": tokens}
                    if tokens > 32000:
                        raise RuntimeError("model usage limit reached")
                    outputs = [item.model_dump(exclude_none=True) for item in (response.output or [])]
                    calls = [item for item in outputs if item["type"] == "function_call"]
                    if stream_shape is not None:
                        tools.record("provider", "OBSERVED", "FIRST_RESPONSE_SHAPE", {
                            **stream_shape,
                            "function_call_count": len(calls),
                            "bravo_namespace_matches": all(call.get("namespace") == "bravo" for call in calls)
                            if calls else None,
                        })
                    if len(calls) != 1:
                        raise RuntimeError("expected one allowed function call")
                    history.extend(outputs)
                    item = calls[0]
                    if self.subscription and item.get("namespace") != "bravo":
                        raise RuntimeError("unexpected subscription tool namespace")
                    if needs_read and item.get("name") != "repo_read":
                        raise RuntimeError("subscription must read the snapshot before proposing")
                    name = FUNCTIONS.get(item["name"], "unlisted")
                    try:
                        if len(item["arguments"]) > 140000:
                            raise ValueError("arguments too large")
                        arguments = json.loads(item["arguments"])
                        result = tools.dispatch(name, arguments)
                        needs_read = False
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
