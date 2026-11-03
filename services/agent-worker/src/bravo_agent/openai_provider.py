"""OpenAI Responses tool loop; every requested action goes through shared policy."""

import asyncio
import copy
import json
import os
import re

from openai import AsyncOpenAI

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
    def __init__(self, model: str):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model):
            raise ValueError("invalid model identifier")
        self.model = model

    async def propose(self, bundle: FailureBundle, tools: Tools) -> None:
        if (set(os.environ) - RUNTIME_KEYS or "ANTHROPIC_API_KEY" in os.environ
                or not os.environ.get("OPENAI_API_KEY")):
            raise RuntimeError("worker process environment must be explicitly isolated")
        history = [{"role": "user", "content": json.dumps({
            "failure": bundle.model_dump(), "snapshot": tools.snapshot.manifest.model_dump(),
        }, sort_keys=True)}]
        definitions = function_tools()
        tokens = 0
        async with asyncio.timeout(180):
            async with AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"],
                                   base_url="https://api.openai.com/v1",
                                   max_retries=0, timeout=60) as client:
                for index in range(8):
                    request = {"model": self.model, "instructions": SYSTEM,
                               "input": history, "tools": definitions, "tool_choice": "auto",
                               "parallel_tool_calls": False, "store": False,
                               "include": ["reasoning.encrypted_content"],
                               "max_output_tokens": min(4096, 32000 - tokens)}
                    if tokens >= 32000 or len(json.dumps(request).encode()) > 131072:
                        raise RuntimeError("model input or token budget reached")
                    response = await client.responses.create(**request)
                    if response.status != "completed" or response.usage is None:
                        raise RuntimeError("incomplete model response or unknown usage")
                    if type(response.usage.total_tokens) is not int or response.usage.total_tokens < 0:
                        raise RuntimeError("invalid model usage")
                    tokens += response.usage.total_tokens
                    tools.usage = {"provider": "openai", "model": self.model,
                                   "requests": index + 1, "total_tokens": tokens}
                    if tokens > 32000:
                        raise RuntimeError("model usage limit reached")
                    outputs = [item.model_dump(exclude_none=True) for item in response.output]
                    calls = [item for item in outputs if item["type"] == "function_call"]
                    if len(calls) != 1:
                        raise RuntimeError("expected one allowed function call")
                    history.extend(outputs)
                    item = calls[0]
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
