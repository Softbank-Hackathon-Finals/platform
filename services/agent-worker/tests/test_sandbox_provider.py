"""Agent SDK sandbox against a real Docker container with a scripted model (no model API)."""

import asyncio
import json
import shutil
import subprocess

import pytest
from agents import Model, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from bravo_agent.coordinator import Coordinator
from bravo_agent.sandbox_provider import SandboxProvider

IMAGE = "python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7"  # 3.12-alpine


def docker_ready():
    if shutil.which("docker") is None:
        return False
    try:
        import docker

        docker.from_env().ping()
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0 or \
            subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True, timeout=180).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not docker_ready(), reason="Docker with the pinned test image is required")


class Scripted(Model):
    """Plays fixed tool calls and keeps every tool output it receives."""

    def __init__(self, calls):
        self.calls, self.outputs = list(calls), []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, **kwargs):
        self.outputs = [item.get("output") for item in input if isinstance(item, dict)
                        and item.get("type") == "function_call_output"]
        self.offered = sorted(tool.name for tool in tools)
        name, arguments = self.calls.pop(0)
        call = ResponseFunctionToolCall(type="function_call", call_id=f"call-{len(self.calls)}", name=name,
                                        arguments=json.dumps(arguments))
        return ModelResponse(output=[call], usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


def run(context, calls):
    bundle, snapshot, proposal, _ = context
    model = Scripted([*calls, ("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(SandboxProvider(model, IMAGE)))
    return result, model


def test_agent_inspects_edits_and_tests_in_the_sandbox_then_proposes(context, tmp_path):
    original = (context[1].root / "src/config.py").read_text()
    result, model = run(context, [
        ("exec_command", {"cmd": "cat src/config.py"}),
        ("exec_command", {"cmd": "printf 'import os\\n' > src/config.py && python3 -c \"import src.config\" "
                                 "&& echo TESTED"}),
    ])
    assert result["state"] == "PROPOSAL_READY"
    joined = "\n".join(str(o) for o in model.outputs)
    assert 'DB_HOST = "localhost"' in joined and "TESTED" in joined
    assert {"exec_command", "plan_propose"} <= set(model.offered)
    # The container edit never reaches the host snapshot; only the proposal leaves.
    assert (context[1].root / "src/config.py").read_text() == original
    tools = [e["tool"] for e in result["audit"]]
    assert tools.count("sandbox.exec") == 2 and "plan.propose" in tools


def test_no_network_and_destructive_commands_are_blocked(context):
    result, model = run(context, [
        ("exec_command", {"cmd": "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3)\" "
                                 "|| echo NO_NETWORK"}),
        ("exec_command", {"cmd": "rm -rf /workspace"}),
    ])
    assert result["state"] == "PROPOSAL_READY"
    joined = "\n".join(str(o) for o in model.outputs)
    assert "NO_NETWORK" in joined and "POLICY_DENIED" in joined
    blocked = [e for e in result["audit"] if e["reason"] == "DESTRUCTIVE_ATTEMPT_BLOCKED"]
    assert blocked and blocked[0]["tool"] == "sandbox.exec" and "rm -rf" not in json.dumps(result["audit"])


def test_unpinned_image_is_refused():
    with pytest.raises(ValueError):
        SandboxProvider(object(), "python:3.12-alpine")


def test_openai_transport_offers_sandbox_tools_and_runs_them_in_docker(context, monkeypatch):
    from test_openai import install_client, response

    from bravo_agent.openai_provider import OpenAIProvider

    bundle, snapshot, proposal, _ = context
    calls, _ = install_client(monkeypatch, [response("exec_command", {"cmd": "cat src/config.py"}),
                                            response("plan_propose", proposal)])
    provider = OpenAIProvider("fixture-model", reasoning_effort="medium", sandbox_image=IMAGE)
    result = asyncio.run(Coordinator(bundle, snapshot).propose(provider))
    assert result["state"] == "PROPOSAL_READY"
    offered = {tool["name"] for tool in calls[0]["tools"]}
    assert {"exec_command", "plan_propose"} <= offered and calls[0]["reasoning"] == {"effort": "medium"}
    outputs = [item for item in calls[1]["input"] if item.get("type") == "function_call_output"]
    assert 'DB_HOST = \\"localhost\\"' in json.dumps(outputs) or 'DB_HOST = "localhost"' in str(outputs)
    assert "sandbox.exec" in [e["tool"] for e in result["audit"]]
