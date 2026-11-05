import asyncio
import json
import tomllib
from types import SimpleNamespace

import pytest

from bravo_agent.account_smoke import (
    AccountSession,
    AccountSmokeProvider,
    IncompleteCodexInstall,
    configuration,
    installed_codex_path,
    run,
)
from bravo_agent.coordinator import Coordinator


class Session:
    def __init__(self, runtime, events):
        self.runtime, self.events = runtime, iter(events)
        self.sent = []

    async def request(self, method, params):
        self.sent.append((method, params))
        if method == "thread/start":
            return {"model": "test-model", "approvalPolicy": "never", "thread": {"id": "thread"}}
        return {"turn": {"id": "turn"}}

    async def receive(self):
        return next(self.events)

    async def send(self, message):
        self.sent.append(message)


def tool_event(name, arguments):
    return {"id": 8, "method": "item/tool/call", "params": {"threadId": "thread",
        "turnId": "turn", "tool": name, "arguments": arguments}}


def completed():
    return {"method": "turn/completed", "params": {"threadId": "thread",
        "turn": {"id": "turn", "status": "completed"}}}


def usage(tokens=200):
    return {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread",
        "tokenUsage": {"total": {"totalTokens": tokens}}}}


def test_config_has_ephemeral_auth_and_no_execution():
    config = tomllib.loads(configuration())
    assert config["cli_auth_credentials_store"] == "ephemeral"
    assert config["forced_login_method"] == "chatgpt"
    assert config["web_search"] == "disabled"
    assert not config["skills"]["bundled"]["enabled"]
    assert config["features"]["code_mode_host"] is True
    for name in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent", "code_mode"):
        assert config["features"][name] is False


@pytest.mark.parametrize("host_mode", [None, 0o644])
def test_incomplete_install_rejected_before_login_or_model_call(tmp_path, monkeypatch, host_mode):
    binary = tmp_path / "codex"
    binary.write_text("installer fixture")
    binary.chmod(0o755)
    if host_mode is not None:
        host = tmp_path / "codex-code-mode-host"
        host.write_text("non-executable fixture")
        host.chmod(host_mode)

    async def unexpected_process(*args, **kwargs):
        pytest.fail("Incomplete installation must not start authentication or a model process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_process)
    args = SimpleNamespace(codex=binary, output=tmp_path / "result.json")
    with pytest.raises(IncompleteCodexInstall):
        asyncio.run(run(args))
    assert not args.output.exists()


def test_installation_follows_cli_link_to_companion_directory(tmp_path):
    install = tmp_path / "release"
    install.mkdir()
    for name in ("codex", "codex-code-mode-host"):
        path = install / name
        path.write_text("installer fixture")
        path.chmod(0o755)
    link = tmp_path / "codex"
    link.symlink_to(install / "codex")
    assert installed_codex_path(link) == install / "codex"


def test_subscription_tools_share_existing_policy(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    session = Session(tmp_path, [tool_event("repo_read", {"path": "src/config.py"}),
                                tool_event("plan_propose", proposal), usage(), completed()])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(AccountSmokeProvider(session, "test-model")))
    assert result["state"] == "PROPOSAL_READY"
    assert [e["tool"] for e in result["audit"]] == ["repo.read", "plan.propose", "model"]
    thread = session.sent[0][1]
    assert thread["environments"] == [] and thread["ephemeral"]
    assert {tool["name"] for tool in thread["dynamicTools"]} == {"repo_read", "plan_propose", "policy_evaluate"}
    assert not thread["allowProviderModelFallback"]


@pytest.mark.parametrize("event", [
    tool_event("shell", {"command": "arbitrary"}),
    {"id": 99, "method": "item/commandExecution/requestApproval", "params": {}},
    {"method": "item/started", "params": {"item": {"type": "commandExecution"}}},
    {"method": "turn/completed", "params": {"threadId": "wrong", "turn": {"status": "completed"}}},
    usage(32001), usage(True), usage(-1),
])
def test_unsafe_or_unknown_events_never_produce_proposal(context, tmp_path, event):
    bundle, snapshot, _, _ = context
    session = Session(tmp_path, [event, completed()])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(AccountSmokeProvider(session, "test-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None
    assert "arbitrary" not in json.dumps(result)


def test_bad_proposal_is_denied_without_raw_data_in_audit(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    proposal["edits"][0]["replacement"] = 'DROP DATABASE sensitive_database;'
    session = Session(tmp_path, [tool_event("plan_propose", proposal), usage(), completed()])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(AccountSmokeProvider(session, "test-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert "sensitive_database" not in json.dumps(result)
    assert result["audit"][0]["outcome"] == "DENIED"


def test_missing_usage_does_not_validate_a_proposal(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    session = Session(tmp_path, [tool_event("plan_propose", proposal), completed()])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(AccountSmokeProvider(session, "test-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None


def test_completed_text_without_tool_proposal_remains_human(context, tmp_path):
    bundle, snapshot, _, _ = context
    message = {"method": "item/completed", "params": {"item": {
        "type": "agentMessage", "text": "private-text-claims-proposal-ready"}}}
    session = Session(tmp_path, [message, usage(), completed()])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(AccountSmokeProvider(session, "test-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["proposal"] is None
    assert result["attempts"][0]["model_usage"]["total_tokens"] == 200
    assert "private-text-claims-proposal-ready" not in json.dumps(result)


def test_subscription_quota_is_reported_without_error_text(context, tmp_path):
    bundle, snapshot, _, _ = context
    event = {"method": "error", "params": {"error": {
        "codexErrorInfo": "usageLimitExceeded", "message": "private-provider-detail"}}}
    result = asyncio.run(Coordinator(bundle, snapshot).propose(
        AccountSmokeProvider(Session(tmp_path, [event]), "test-model")))
    assert result["state"] == "NEEDS_HUMAN"
    assert result["audit"][0]["category"] == "usageLimitExceeded"
    assert "private-provider-detail" not in json.dumps(result)


def test_rpc_preserves_events_that_arrive_before_response(tmp_path):
    async def exercise():
        session = AccountSession(None, tmp_path)
        event = usage()
        messages = iter([event, {"id": 1, "result": {"ok": True}}])

        async def read():
            return next(messages)

        async def send(message):
            assert message["method"] == "turn/start"

        session._read, session.send = read, send
        assert await session.request("turn/start", {}) == {"ok": True}
        assert await session.receive() == event

    asyncio.run(exercise())
