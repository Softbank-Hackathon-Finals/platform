"""OpenAI Agents SDK runtime with the bounded SIWC Responses transport adapter."""

import asyncio
import copy
import json
import os
import re
from collections import Counter
from typing import get_args

from agents import Agent, FunctionTool, Model, ModelResponse, RunConfig, Runner, tool_namespace
from agents.agent import ToolsToFinalOutputResult
from agents.tracing import generation_span
from agents.usage import Usage
from openai import APIStatusError, AsyncOpenAI

from .contracts import FailureBundle, Proposal
from .runtime import RUNTIME_KEYS
from .telemetry import agent_observation
from .tools import RESERVED_TOOLS, SANDBOX_TOOLS, SYSTEM, PolicyDenied, Tools

FUNCTIONS = {"repo_read": "mcp__bravo__repo_read", "plan_propose": "mcp__bravo__plan_propose",
             "inventory_describe": "mcp__bravo__inventory_describe", "policy_evaluate": "mcp__bravo__policy_evaluate"}
# Reserved (not yet provided) tools are never offered, but a model may still name them. Map the
# function-name spellings a model can emit to the same canonical name the Claude hook sees, so the
# denial is classified once (TOOL_NOT_PROVIDED / DESTRUCTIVE_ATTEMPT_BLOCKED) instead of "unlisted".
RESERVED_FUNCTIONS = {
    form: tool
    for tool in RESERVED_TOOLS if tool.startswith("mcp__bravo__")
    for base in [tool.removeprefix("mcp__bravo__")]
    for form in (base, base.replace("_", ".", 1), "bravo." + base, "bravo." + base.replace("_", ".", 1), tool)
}
BRAVO_KINDS = frozenset("bravo_" + name for name in FUNCTIONS) | {"bravo_reserved"}


def tool_name(name, namespace=None, *, subscription=False) -> str:
    """Canonical policy name for a model function call. Offered tools only inside the bravo
    namespace on the subscription path; reserved spellings anywhere; everything else "unlisted"."""
    if not isinstance(name, str):
        return "unlisted"
    if name in FUNCTIONS and (not subscription or namespace == "bravo"):
        return FUNCTIONS[name]
    return RESERVED_FUNCTIONS.get(name, "unlisted")


def call_arguments(raw):
    """(parsed, value). Unparseable or oversized text is returned for classification only."""
    if not isinstance(raw, str):
        return False, raw
    if len(raw) > 140000:
        return False, raw[:140000]
    try:
        return True, json.loads(raw)
    except ValueError:
        return False, raw


def deny_unprovided(tools: Tools, name: str, arguments) -> None:
    """Record the shared denial for a call that will not run. ``name`` is never an offered tool
    here, so dispatch only classifies; the arguments are inspected, never stored."""
    if tools.allowed(name):
        raise RuntimeError("refusal path reached an offered tool")
    try:
        tools.dispatch(name, arguments)
    except PolicyDenied:
        return
    raise RuntimeError("refused call was not denied")


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


def function_tools(tools: Tools | None = None) -> list[dict]:
    """``inventory_describe`` is offered only when the control path attached a T16 summary."""
    definitions = [
        {"type": "function", "name": "repo_read", "strict": True,
         "description": "Read an approved application snapshot file.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False}},
        {"type": "function", "name": "plan_propose", "strict": True,
         "description": "Submit a bounded correction; does not apply or verify it.",
         "parameters": strict_schema(Proposal.model_json_schema())},
        {"type": "function", "name": "policy_evaluate", "strict": True,
         "description": "Lint a candidate policy or replacement text with the same checks plan_propose applies. No side effects.",
         "parameters": {"type": "object", "properties": {"policy": {"type": "string"}},
                        "required": ["policy"], "additionalProperties": False}},
    ]
    if tools is not None and tools.inventory is not None:
        definitions.append(
            {"type": "function", "name": "inventory_describe", "strict": True,
             "description": "Read the graded dependency inventory (T16). Optional role filter, null for all.",
             "parameters": {"type": "object", "properties": {"role": {"type": ["string", "null"]}},
                            "required": ["role"], "additionalProperties": False}})
    return definitions


def output_kind(item) -> str:
    """Return a code-owned label; never retain response text, names, IDs or arguments."""
    kind = getattr(item, "type", None)
    if kind == "function_call":
        if getattr(item, "namespace", None) != "bravo":
            return "other_function"
        for name in FUNCTIONS:
            if getattr(item, "name", None) == name:
                return "bravo_" + name
        # Bound like an offered call so it reaches dispatch and is denied and audited there.
        return "bravo_reserved" if getattr(item, "name", None) in RESERVED_FUNCTIONS else "other_function"
    if kind == "message":
        content = getattr(item, "content", None)
        return "refusal" if isinstance(content, list) and any(getattr(p, "type", None) == "refusal" for p in content) else "message"
    return kind if kind in ("reasoning", "custom_tool_call", "tool_search_call") else "other"



def flat_shape(stream_shape: dict, calls: list[dict]) -> dict:
    """FIRST_RESPONSE_SHAPE as scalar fields only (str/int/bool), so T15 and T38 can carry the
    attempt's tool events. Counters become ``kind=n`` lists; the rejected stream event becomes
    one ``kind:phase:fields:other`` string. Every part is a code-owned label, never model text."""
    def counts(counter) -> str:
        return ",".join(f"{kind}={n}" for kind, n in sorted(dict(counter or {}).items()))

    shape = {"stream_added": counts(stream_shape.get("stream_added")),
             "stream_done": counts(stream_shape.get("stream_done")),
             "function_call_count": len(calls)}
    if "terminal_output_missing" in stream_shape:
        shape["terminal_output_missing"] = stream_shape["terminal_output_missing"]
    if "terminal_items" in stream_shape:
        shape["terminal_items"] = counts(stream_shape["terminal_items"])
    if "stream_resolution" in stream_shape:
        shape["stream_resolution"] = stream_shape["stream_resolution"]
    rejected = stream_shape.get("rejected_event")
    if rejected is not None:
        shape["rejected_event"] = ":".join([rejected["kind"], rejected["phase"], "+".join(rejected["fields"]),
                                            "other" if rejected["other_fields"] else ""])
    if calls:
        shape["bravo_namespace_matches"] = all(call.get("namespace") == "bravo" for call in calls)
    return shape

class FinalizedStreamCall:
    """Keep one matched function item for a completed response with an explicit empty output."""
    def __init__(self, *, diagnose=False):
        self.diagnose = diagnose
        self.response_id = self.added = self.index = self.done = self.final_args = None
        self.problem = None
        self.rejected_event = None

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
            if (not self.response_id or output_kind(item) not in BRAVO_KINDS
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
            fields = (event.model_fields_set | (event.model_extra or {}).keys()) - {"type", "sequence_number"}
            # A reply annotation carries no function output. It cannot complete a call or enter model history.
            if (event.type == "response.metadata" and self.added is not None and self.done is None
                    and fields == {"response_id", "metadata"} and isinstance(self.response_id, str)
                    and event.response_id == self.response_id and isinstance(event.metadata, dict)):
                return
            self.problem = "unrecognized_event"
            if not self.diagnose:
                return
            # The SDK can construct a model even for an unknown wire discriminator.
            # Only report SDK literals or fixed protocol labels, never provider-supplied names or values.
            literals = get_args(type(event).model_fields["type"].annotation)
            extensions = ("response.metadata", "codex.response.metadata", "codex.rate_limits",
                          "responsesapi.websocket_timing", "ping", "keepalive")
            known = {"response", "response_id", "item", "item_id", "output_index", "content_index",
                     "part", "delta", "arguments", "headers", "metadata", "rate_limits", "safety_buffering"}
            phase = "before_response"
            if self.done is not None:
                phase = "after_done"
            elif self.added is not None:
                phase = "after_added"
            elif self.response_id is not None:
                phase = "after_created"
            self.rejected_event = {"kind": event.type if event.type in (*literals, *extensions) else "other",
                                   "phase": phase, "fields": sorted(fields & known),
                                   "other_fields": bool(fields - known)}

    def resolved(self, response):
        if (self.problem or not self.response_id or response.id != self.response_id
                or response.status != "completed" or response.error or response.incomplete_details):
            return None, self.problem or "unbound_response"
        if self.done is None:
            return None, "no_finalized_call"
        return self.done, "matched_finalized_call"


class OpenAIProvider:
    def __init__(self, model: str, *, subscription: bool = False, reasoning_effort: str | None = None,
                 sandbox_image: str | None = None):
        from .runtime import effort

        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model):
            raise ValueError("invalid model identifier")
        self.model = model
        self.subscription = subscription
        self.reasoning_effort = effort(reasoning_effort)
        self.sandbox_image = sandbox_image  # validated by SandboxProvider when used

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
            candidate = FinalizedStreamCall(diagnose=stream_shape is not None)
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
                                if candidate.rejected_event is not None:
                                    stream_shape["rejected_event"] = candidate.rejected_event
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
        async with asyncio.timeout(180):
            async with AsyncOpenAI(api_key=os.environ[credential],
                                   base_url="https://api.openai.com/v1",
                                   max_retries=0, timeout=60) as client:
                model = BoundedResponsesModel(self, client, tools)
                if self.sandbox_image is not None:
                    from .sandbox_provider import SandboxProvider

                    # Same bounded subscription/API transport; shell and edits run in the
                    # no-network Docker sandbox, only plan_propose leaves it.
                    with agent_observation(tools, "openai_agents_sandbox", self.model):
                        await SandboxProvider(model, self.sandbox_image,
                                              platform=model.sdk_tools()).propose(bundle, tools)
                    return
                with agent_observation(tools, "openai_agents", self.model) as observation:
                    async def finish(_context, _results):
                        return ToolsToFinalOutputResult(
                            is_final_output=tools.proposal is not None, final_output="PROPOSAL_COLLECTED")

                    agent = Agent(name="bravo-proposal", instructions=SYSTEM, model=model,
                                  tools=model.sdk_tools(), tool_use_behavior=finish)
                    await Runner.run(
                        agent, [{"role": "user", "content": json.dumps(tools.model_input(), sort_keys=True)}],
                        max_turns=8,
                        run_config=RunConfig(
                            workflow_name="bravo-proposal", trace_id=observation.sdk_trace_id,
                            trace_include_sensitive_data=False,
                            group_id=bundle.run_id,
                            trace_metadata={"source_sha": bundle.source_sha, "attempt": tools.attempt},
                        ),
                    )
                    if tools.proposal is None:
                        raise RuntimeError("no schema-validated proposal")
                    tools.record("provider", "COMPLETED", "PROPOSAL_COLLECTED")


class BoundedResponsesModel(Model):
    """One request per SDK turn, preserving subscription wire and policy contracts.

    Runner owns history, turn limits, function invocation and termination. This adapter only
    translates the SIWC namespace/stream protocol and validates a response before SDK dispatch.
    The unadvertised denial handler cannot execute an action; it returns the existing refusal.
    """

    def __init__(self, provider, client, tools):
        self.provider, self.client, self.policy = provider, client, tools
        self.needs_read = provider.subscription
        self.tokens = self.requests = 0
        self.denied_calls = {}

    def sdk_tools(self):
        definitions = function_tools(self.policy)
        result = []
        for definition in definitions:
            name = definition["name"]

            async def invoke(_context, raw, name=name):
                parsed, arguments = call_arguments(raw)
                canonical = FUNCTIONS[name]
                try:
                    if not parsed:
                        raise ValueError("invalid function arguments")
                    result = self.policy.dispatch(canonical, arguments)
                    self.needs_read = False
                    return json.dumps(result)
                except (ValueError, PolicyDenied):
                    self.policy.record(canonical, "DENIED", "INVALID_FUNCTION_ARGUMENTS")
                    return json.dumps({"error": "POLICY_DENIED"})

            result.append(FunctionTool(name=name, description=definition["description"],
                                       params_json_schema=definition["parameters"], on_invoke_tool=invoke))

        async def denied(_context, _raw):
            return json.dumps({"error": "POLICY_DENIED"})

        result.append(FunctionTool(name="policy_denied", description="Internal refusal result.",
                                   params_json_schema={"type": "object", "properties": {},
                                                       "additionalProperties": False},
                                   on_invoke_tool=denied))
        if self.provider.subscription:
            return tool_namespace(name="bravo", tools=result,
                                  description="Approved snapshot reads and proposal submission only.")
        return result

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id=None, conversation_id=None,
                           prompt=None):
        if previous_response_id or conversation_id or handoffs or output_schema or prompt:
            raise RuntimeError("unsupported model continuation")
        history = copy.deepcopy(input)
        # Preserve the original wire function identity after the SDK's internal refusal handler.
        for item in history:
            if item.get("type") == "function_call" and item.get("call_id") in self.denied_calls:
                original = self.denied_calls[item["call_id"]]
                item.clear()
                item.update(original)
        definitions = function_tools(self.policy)
        # Agents SDK sandbox capability tools (exec_command, apply_patch, ...) act only inside
        # the no-network container; offer exactly the ones this run attached.
        self.sandbox_names = {tool.name for tool in tools or ()
                              if tool.name in SANDBOX_TOOLS and isinstance(tool, FunctionTool)}
        definitions += [{"type": "function", "name": tool.name, "description": tool.description,
                         "parameters": tool.params_json_schema, "strict": tool.strict_json_schema}
                        for tool in tools or () if tool.name in self.sandbox_names]
        if self.needs_read:
            definitions = [item for item in definitions if item["name"] == "repo_read"]
        if self.provider.subscription:
            definitions = [{"type": "namespace", "name": "bravo", "tools": definitions,
                            "description": "Approved snapshot reads and proposal submission only."}]
        request = {"model": self.provider.model, "instructions": system_instructions,
                   "input": history, "tools": definitions,
                   "tool_choice": "required" if self.needs_read else "auto",
                   "parallel_tool_calls": False, "store": False,
                   "include": ["reasoning.encrypted_content"]}
        if self.provider.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.provider.reasoning_effort}
        if not self.provider.subscription:
            request["max_output_tokens"] = min(4096, 32000 - self.tokens)
        if self.tokens >= 32000 or len(json.dumps(request).encode()) > 131072:
            raise RuntimeError("model input or token budget reached")
        shape = {"stream_added": Counter(), "stream_done": Counter()} if self.provider.subscription and not self.requests else None
        with generation_span(model=self.provider.model, disabled=tracing.is_disabled()) as span:
            response = await self.provider.response(self.client, request, self.policy, shape)
            if response.status != "completed" or response.usage is None:
                raise RuntimeError("incomplete model response or unknown usage")
            total = response.usage.total_tokens
            if type(total) is not int or total < 0:
                raise RuntimeError("invalid model usage")
            self.tokens += total
            self.requests += 1
            self.policy.usage = {"provider": "chatgpt_plan" if self.provider.subscription else "openai",
                                 "model": self.provider.model, "requests": self.requests,
                                 "total_tokens": self.tokens}
            if self.tokens > 32000:
                raise RuntimeError("model usage limit reached")
            usage = Usage(requests=1, total_tokens=total,
                          input_tokens=getattr(response.usage, "input_tokens", 0),
                          output_tokens=getattr(response.usage, "output_tokens", 0))
            span.span_data.usage = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                                    "total_tokens": total}
            outputs = [item.model_dump(exclude_none=True) for item in response.output or []]
            calls = [item for item in outputs if item["type"] == "function_call"]
            if shape is not None:
                self.policy.record("provider", "OBSERVED", "FIRST_RESPONSE_SHAPE", flat_shape(shape, calls))
            if any(item.get("type") not in {"function_call", "reasoning", "message"} for item in outputs):
                # No hosted or executable built-in tools are permitted on this proposal path.
                raise RuntimeError("unexpected model output kind")
            if len(calls) != 1:
                raise RuntimeError("expected one allowed function call")
            item = calls[0]
            name = tool_name(item.get("name"), item.get("namespace"), subscription=self.provider.subscription)
            _parsed, arguments = call_arguments(item.get("arguments"))
            if item.get("name") in getattr(self, "sandbox_names", ()) and (
                    not self.provider.subscription or item.get("namespace") == "bravo") and not self.needs_read:
                item.pop("namespace", None)  # the SDK owns these tools outside the bravo namespace
                return ModelResponse(output=outputs, usage=usage, response_id=getattr(response, "id", None))
            if (self.provider.subscription and item.get("namespace") != "bravo"
                    or self.needs_read and item.get("name") != "repo_read"):
                if not self.policy.allowed(name):
                    deny_unprovided(self.policy, name, arguments)
                raise RuntimeError("subscription tool ordering or namespace rejected")
            if not self.policy.allowed(name):
                deny_unprovided(self.policy, name, arguments)
                self.denied_calls[item["call_id"]] = dict(item)
                item.update(name="policy_denied", arguments="{}")
            return ModelResponse(output=outputs, usage=usage, response_id=getattr(response, "id", None))

    async def stream_response(self, *args, **kwargs):
        raise RuntimeError("Runner must use bounded completed responses")
        yield  # Implements Model's async iterator interface without exposing partial tool calls.
