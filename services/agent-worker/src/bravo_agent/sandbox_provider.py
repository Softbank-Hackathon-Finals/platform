"""Agent SDK sandbox: the model loop and its key stay here; shell and file edits run in a
no-network Docker container that holds only a copy of the approved snapshot.

Kickoff requirement: the agent may freely inspect, edit and test in a sandbox, and nothing
leaves it except a schema-validated proposal (`plan_propose`), which the existing policy,
T15 rehearsal and human approval still decide on. Built from the OpenAI Agents SDK public
surface: `SandboxAgent` + `RunConfig(sandbox=...)` (Docker client, `network_mode="none"`),
`tool_input_guardrail` for destructive commands, `RunHooks` for the trajectory and per-tool
`timeout_seconds`. Container edits are never copied back.
"""

import json
import re

from agents import (
    FunctionTool,
    RunConfig,
    RunHooks,
    Runner,
    ToolGuardrailFunctionOutput,
    tool_input_guardrail,
)
from agents.agent import ToolsToFinalOutputResult
from agents.run_config import SandboxRunConfig
from agents.sandbox import Manifest, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File

from .contracts import FailureBundle
from .tools import SYSTEM, PolicyDenied, Tools, destructive_category

IMAGE = re.compile(r"[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[a-f0-9]{64}")
SANDBOX_SYSTEM = """

You have a sandbox: /workspace holds a copy of the approved snapshot. You may read, edit and
run commands there (tests, linters, the app). It has no network and no credentials. Nothing
you change there is applied anywhere. To propose a change, call plan_propose with the full
replacement text of each changed file; that proposal is checked again outside the sandbox.
"""


def manifest_for(tools: Tools) -> Manifest:
    """The snapshot copy only; no host paths, mounts or environment."""
    return Manifest(root="/workspace", entries={
        name: File(content=tools.snapshot.read(name).encode()) for name in tools.snapshot.manifest.files})


def destructive_guardrail(tools: Tools):
    @tool_input_guardrail
    def guard(data):
        try:
            arguments = json.loads(data.context.tool_arguments or "{}")
        except ValueError:
            arguments = data.context.tool_arguments
        category = destructive_category(arguments)
        if category:
            # Same audit contract as the platform tools (ADR-0019); arguments are never stored.
            tools.record(data.context.tool_name, "DENIED", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": category})
            return ToolGuardrailFunctionOutput.reject_content("POLICY_DENIED")
        return ToolGuardrailFunctionOutput.allow()
    return guard


class TrajectoryHooks(RunHooks):
    """Every sandbox tool call becomes a code-owned audit event (the trajectory)."""

    def __init__(self, tools: Tools):
        self.tools = tools

    async def on_tool_end(self, context, agent, tool, result):
        if tool.name in {"exec_command", "write_stdin", "apply_patch", "view_image"}:
            self.tools.record(tool.name, "ALLOWED", "SANDBOX_TOOL_CALL")


def platform_tools(tools: Tools) -> list[FunctionTool]:
    """plan_propose / policy_evaluate / inventory_describe, dispatched through the same policy."""
    from .openai_provider import FUNCTIONS, call_arguments, function_tools

    result = []
    for definition in function_tools(tools):
        name = definition["name"]
        if name not in FUNCTIONS:
            continue  # T42 rehearsal exec stays on its own path

        async def invoke(_context, raw, name=name):
            parsed, arguments = call_arguments(raw)
            try:
                if not parsed:
                    raise ValueError("invalid function arguments")
                return json.dumps(tools.dispatch(FUNCTIONS[name], arguments))
            except (ValueError, PolicyDenied):
                tools.record(FUNCTIONS[name], "DENIED", "INVALID_FUNCTION_ARGUMENTS")
                return json.dumps({"error": "POLICY_DENIED"})

        result.append(FunctionTool(name=name, description=definition["description"],
                                   params_json_schema=definition["parameters"], on_invoke_tool=invoke,
                                   timeout_seconds=30))
    return result


class SandboxProvider:
    """A `Provider` for the Coordinator. `model` is any Agents SDK Model (subscription or API key)."""

    def __init__(self, model, image, *, platform=None, docker_client=None, max_turns=12):
        if not isinstance(image, str) or IMAGE.fullmatch(image) is None:
            raise ValueError("sandbox image must be pinned by digest")
        self.model, self.image, self.max_turns = model, image, max_turns
        self.platform = platform  # the transport's own platform tools (e.g. subscription namespace)
        self.docker_client = docker_client

    def _client(self):
        from agents.sandbox.sandboxes.docker import DockerSandboxClient

        if self.docker_client is None:
            import docker

            self.docker_client = docker.from_env()
        return DockerSandboxClient(self.docker_client)

    async def propose(self, bundle: FailureBundle, tools: Tools) -> None:
        from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

        guard = destructive_guardrail(tools)

        def guard_shell(toolset):
            toolset.exec_command.tool_input_guardrails = [guard]
            if toolset.write_stdin is not None:
                toolset.write_stdin.tool_input_guardrails = [guard]

        async def finish(_context, _results):
            return ToolsToFinalOutputResult(is_final_output=tools.proposal is not None,
                                            final_output="PROPOSAL_COLLECTED")

        agent = SandboxAgent(name="bravo-sandbox-proposal", instructions=SYSTEM + SANDBOX_SYSTEM,
                             model=self.model, tool_use_behavior=finish,
                             tools=self.platform if self.platform is not None else platform_tools(tools),
                             default_manifest=manifest_for(tools),
                             # Shell only: the smallest whitelist that can read, edit and test.
                             capabilities=[Shell(configure_tools=guard_shell)])
        config = RunConfig(
            sandbox=SandboxRunConfig(client=self._client(), options=DockerSandboxClientOptions(
                image=self.image, network_mode="none", labels={"bravo.dev/run-id": bundle.run_id[:63]})),
            tracing_disabled=True, trace_include_sensitive_data=False, workflow_name="bravo-sandbox-proposal")
        await Runner.run(agent, json.dumps(tools.model_input(), sort_keys=True), max_turns=self.max_turns,
                         hooks=TrajectoryHooks(tools), run_config=config)
        if tools.proposal is None:
            raise RuntimeError("no schema-validated proposal")
        tools.record("provider", "COMPLETED", "PROPOSAL_COLLECTED")
