import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from bravo_agent.cli import isolated_environment
from bravo_agent.sdk import ClaudeProvider, guard, make_options
from bravo_agent.tools import TOOL_NAMES


def test_sdk_command_and_options_disable_ambient_authority(context, tmp_path):
    options = make_options(context[3], tmp_path, "explicit-model")
    transport = SubprocessCLITransport(prompt="test", options=options)
    # Command construction only: no provider process/network is started by this test.
    transport._cli_path = "/fixture/claude"
    command = transport._build_command()
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--setting-sources=" in command
    assert options.skills == []
    assert options.permission_mode == "dontAsk"
    assert set(options.allowed_tools) == set(TOOL_NAMES)
    assert options.cwd == tmp_path
    assert options.output_format is None  # no hidden StructuredOutput tool
    assert options.hooks["PreToolUse"][0].matcher is None
    assert options.max_turns == 8
    assert options.max_budget_usd == 0.5


def test_hook_checks_all_tools_and_does_not_echo_inputs(context):
    tools = context[3]
    denied = asyncio.run(guard(tools, {"tool_name": "Bash", "tool_input": "secret"}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    for name in TOOL_NAMES:
        allowed = asyncio.run(guard(tools, {"tool_name": name}, None, None))
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "secret" not in str(tools.events)


def test_cli_replaces_environment_instead_of_relying_on_sdk_env(tmp_path):
    original = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "model-only-fixture",
                "AWS_SECRET_ACCESS_KEY": "production-fixture", "GH_TOKEN": "github-fixture",
                "HOME": "/developer", "CLAUDE_CODE_OAUTH_TOKEN": "personal-login-fixture"}
    result = isolated_environment(original, tmp_path, "claude")
    assert result["ANTHROPIC_API_KEY"] == "model-only-fixture"
    assert result["HOME"] == str(tmp_path)
    assert result["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert not {"AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} & result.keys()


def test_sdk_result_requires_valid_proposal_and_success(context, tmp_path, monkeypatch):
    from bravo_agent import sdk

    bundle, _, proposal, tools = context
    monkeypatch.setattr(sdk.os, "environ", {"ANTHROPIC_API_KEY": "model-fixture"})

    class Client:
        def __init__(self, **kwargs):
            self.options = kwargs["options"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            assert "DB_CONNECTION_FAILED" in prompt
            tools.dispatch("mcp__bravo__plan_propose", proposal)

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="fixture")

    monkeypatch.setattr(sdk, "ClaudeSDKClient", Client)
    asyncio.run(ClaudeProvider(tmp_path, "explicit-model").propose(bundle, tools))
    assert tools.proposal is not None


@pytest.mark.parametrize("name, tool_input, reason, category", [
    ("Bash", {"command": "aws s3 rb s3://synthetic-review-bucket --force"}, "DESTRUCTIVE_ATTEMPT_BLOCKED", "bucket_delete"),
    ("Bash", {"command": "kubectl delete namespace rehearsal-1"}, "DESTRUCTIVE_ATTEMPT_BLOCKED", "namespace_delete"),
    ("mcp__bravo__rehearsal_exec", {"command": "ls"}, "TOOL_NOT_PROVIDED", None),
    ("WebFetch", {"url": "https://example.invalid"}, "TOOL_NOT_ALLOWED", None),
])
def test_pre_tool_use_hook_records_the_same_audit_classification_as_dispatch(context, name, tool_input, reason, category):
    tools = context[3]
    denied = asyncio.run(guard(tools, {"tool_name": name, "tool_input": tool_input}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    event = tools.events[-1]
    assert event["outcome"] == "DENIED" and event["reason"] == reason and event.get("category") == category
    assert "synthetic-review-bucket" not in json.dumps(tools.events) and "rehearsal-1" not in json.dumps(tools.events)
