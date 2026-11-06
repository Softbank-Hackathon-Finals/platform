"""Optional Claude adapter. MCP handlers enforce policy even without hooks."""

import asyncio
import json
import os
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from .contracts import FailureBundle, Proposal
from .runtime import RUNTIME_KEYS
from .telemetry import agent_observation, tool_observation
from .tools import SYSTEM, TOOL_NAMES, PolicyDenied, Tools


async def guard(tools: Tools, data: dict, _tool_use_id, _context):
    name = data.get("tool_name", "")
    allowed = tools.allowed(name)
    if not allowed:
        # Same classification as Tools.dispatch so the ADR-0019 audit contract holds on this path
        # too; tool_input is classified, never recorded.
        reason, metadata = tools.classify_denial(name, data.get("tool_input"))
        tools.record(name, "DENIED", reason, metadata)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": "exact platform tool allowlist",
    }}


def make_options(tools: Tools, runtime: Path, model: str) -> ClaudeAgentOptions:
    async def call(name: str, args: dict):
        try:
            with tool_observation(name):
                value = tools.dispatch(name, args)
            return {"content": [{"type": "text", "text": json.dumps(value)}]}
        except PolicyDenied:
            return {"content": [{"type": "text", "text": "POLICY_DENIED"}], "isError": True}

    @tool("repo_read", "Read one approved application snapshot file.",
          {"type": "object", "properties": {"path": {"type": "string"}},
           "required": ["path"], "additionalProperties": False})
    async def read(args):
        return await call("mcp__bravo__repo_read", args)

    @tool("plan_propose", "Submit a bounded file replacement proposal; does not apply it.",
          Proposal.model_json_schema())
    async def propose(args):
        return await call("mcp__bravo__plan_propose", args)

    @tool("policy_evaluate", "Lint a candidate policy or replacement text; no side effects.",
          {"type": "object", "properties": {"policy": {"type": "string"}},
           "required": ["policy"], "additionalProperties": False})
    async def evaluate(args):
        return await call("mcp__bravo__policy_evaluate", args)

    @tool("inventory_describe", "Read the graded dependency inventory (T16); optional role filter.",
          {"type": "object", "properties": {"role": {"type": ["string", "null"]}},
           "additionalProperties": False})
    async def describe(args):
        return await call("mcp__bravo__inventory_describe", args)

    handlers = [read, propose, evaluate] + ([describe] if tools.inventory is not None else [])

    async def before(data, tool_use_id, context):
        return await guard(tools, data, tool_use_id, context)

    return ClaudeAgentOptions(
        tools=[], allowed_tools=list(TOOL_NAMES),
        mcp_servers={"bravo": create_sdk_mcp_server(name="bravo", tools=handlers)},
        strict_mcp_config=True, setting_sources=[], skills=[], plugins=[], agents={},
        permission_mode="dontAsk", system_prompt=SYSTEM,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[before])]},
        cwd=runtime, model=model, max_turns=8, max_budget_usd=0.5,
        env={"CLAUDE_CONFIG_DIR": str(runtime / "config"),
             "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        # SDK diagnostics can contain provider data. The coordinator records bounded reason codes.
        stderr=lambda _message: None,
    )


class ClaudeProvider:
    def __init__(self, runtime: Path, model: str):
        self.runtime, self.model = runtime, model

    async def propose(self, bundle: FailureBundle, tools: Tools) -> None:
        # options.env is merged over os.environ by the SDK; it does not replace it.
        if (set(os.environ) - RUNTIME_KEYS or "OPENAI_API_KEY" in os.environ
                or not os.environ.get("ANTHROPIC_API_KEY")):
            raise RuntimeError("worker process environment must be explicitly isolated")
        options = make_options(tools, self.runtime, self.model)
        prompt = json.dumps(tools.model_input(), sort_keys=True)
        with agent_observation(tools, "claude_agent_sdk", self.model):
            await self._run(options, prompt, tools)

    async def _run(self, options, prompt, tools):
        async with asyncio.timeout(180):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                completed = False
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        if message.is_error or message.subtype != "success":
                            raise RuntimeError("model did not complete")
                        usage = message.usage or {}
                        counts = {key: value for key, value in usage.items()
                                  if key in {"input_tokens", "output_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens"}
                                  and type(value) is int and value >= 0}
                        tools.usage = {"provider": "claude", "model": self.model,
                                       "requests": message.num_turns,
                                       "total_tokens": sum(counts.values()), **counts}
                        completed = True
                if not completed or tools.proposal is None:
                    raise RuntimeError("no schema-validated proposal")
