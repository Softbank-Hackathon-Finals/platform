"""Interactive operator validation using official Codex device login, never a hosted API.

Credentials stay inside one ephemeral app-server process. This opt-in smoke path
does not replace the platform's API-key provider or provide unattended service auth.
"""

import argparse
import asyncio
import json
import os
import re
import signal
import tempfile
from collections import deque
from pathlib import Path

from .cli import bounded_json
from .contracts import FailureBundle, SnapshotManifest
from .coordinator import Coordinator
from .openai_provider import FUNCTIONS, function_tools
from .tools import SYSTEM, PolicyDenied, Snapshot

CODEX_VERSION = "codex-cli 0.157.0"
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "hooks",
    "memories", "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host",
    "computer_use", "browser_use", "browser_use_external", "in_app_browser",
    "image_generation", "view_image", "skill_search", "skill_mcp_dependency_install",
    "request_permissions_tool", "goals", "sleep_tool", "workspace_dependencies",
    "tool_suggest", "remote_plugin", "realtime_conversation", "daemon_auto_start",
)


def configuration() -> str:
    return '\n'.join([
        'cli_auth_credentials_store = "ephemeral"', 'forced_login_method = "chatgpt"',
        'approval_policy = "never"', 'sandbox_mode = "read-only"', 'web_search = "disabled"',
        'project_doc_max_bytes = 0', 'model_reasoning_effort = "low"',
        '[skills]', 'include_instructions = false', 'bundled.enabled = false',
        '[features]', 'skip_host_skill_discovery = true',
        *(f'{name} = false' for name in DISABLED_FEATURES),
        '[analytics]', 'enabled = false',
    ]) + '\n'


class AccountSession:
    def __init__(self, process, runtime: Path):
        self.process, self.runtime = process, runtime
        self.sequence = 0
        self.pending = deque()

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def receive(self):
        if self.pending:
            return self.pending.popleft()
        return await self._read()

    async def _read(self):
        raw = await self.process.stdout.readline()
        if not raw or len(raw) > 1048576:
            raise RuntimeError("invalid account protocol stream")
        return json.loads(raw)

    async def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self._read()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    code = message["error"].get("code")
                    print(json.dumps({"state": "PROTOCOL_REJECTED", "method": method,
                                      "code": code if type(code) is int else None}), flush=True)
                    raise RuntimeError("account protocol request rejected")
                return message["result"]
            if "method" not in message or len(self.pending) >= 128:
                raise RuntimeError("unexpected or excessive protocol response")
            self.pending.append(message)

    async def login(self):
        await self.request("initialize", {"clientInfo": {"name": "bravo_operator_smoke",
                            "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized"})
        login = await self.request("account/login/start", {"type": "chatgptDeviceCode"})
        if (login.get("verificationUrl") != "https://auth.openai.com/codex/device"
                or not re.fullmatch(r"[A-Z0-9-]{6,20}", login.get("userCode", ""))):
            raise RuntimeError("unexpected device login instructions")
        # The official one-time user code is the only auth information shown.
        print(json.dumps({"state": "DEVICE_LOGIN_REQUIRED", "verification_url": login[
            "verificationUrl"], "user_code": login["userCode"]}), flush=True)
        async with asyncio.timeout(600):
            while True:
                message = await self.receive()
                if "id" in message:
                    raise RuntimeError("unexpected login request")
                if message.get("method") == "account/login/completed":
                    params = message["params"]
                    if params.get("loginId") != login["loginId"] or not params.get("success"):
                        raise RuntimeError("device login failed")
                    break
        account = await self.request("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise RuntimeError("subscription authentication required")
        if (self.runtime / "auth.json").exists():
            raise RuntimeError("unexpected persisted credentials")
        print('{"state":"SUBSCRIPTION_AUTHENTICATED"}', flush=True)


class AccountSmokeProvider:
    def __init__(self, session: AccountSession, model: str):
        self.session, self.model = session, model

    async def propose(self, bundle, tools):
        self.stage = "THREAD_START"
        try:
            await self._propose(bundle, tools)
        except Exception:
            if self.stage != "MODEL_ERROR":
                tools.record("provider", "STOPPED", "ACCOUNT_" + self.stage + "_FAILED")
            raise

    async def _propose(self, bundle, tools):
        session = self.session
        definitions = [{"type": "function", "name": item["name"],
                        "description": item["description"], "inputSchema": item["parameters"]}
                       for item in function_tools()]
        async with asyncio.timeout(180):
            thread = await session.request("thread/start", {
                "model": self.model, "allowProviderModelFallback": False,
                "cwd": str(session.runtime), "ephemeral": True, "environments": [],
                "approvalPolicy": "never", "sandbox": "read-only",
                "baseInstructions": SYSTEM, "dynamicTools": definitions,
            })
            if thread.get("model") != self.model or thread.get("approvalPolicy") != "never":
                raise RuntimeError("unexpected effective model or approval policy")
            thread_id = thread["thread"]["id"]
            self.stage = "TURN_START"
            turn = await session.request("turn/start", {"threadId": thread_id, "effort": "low",
                "input": [{"type": "text", "text": json.dumps({
                    "failure": bundle.model_dump(), "snapshot": tools.snapshot.manifest.model_dump()
                }, sort_keys=True)}]})
            turn_id = turn["turn"]["id"]
            self.stage = "MODEL_RESPONSE"
            while True:
                message = await session.receive()
                method, params = message.get("method"), message.get("params", {})
                if "id" in message:
                    if (method != "item/tool/call" or params.get("threadId") != thread_id
                            or params.get("turnId") != turn_id or params.get("namespace")):
                        raise RuntimeError("unapproved server request")
                    name = FUNCTIONS.get(params.get("tool"), "unlisted")
                    try:
                        result = tools.dispatch(name, params.get("arguments"))
                        success = True
                    except PolicyDenied:
                        result, success = {"error": "POLICY_DENIED"}, False
                    await session.send({"id": message["id"], "result": {"success": success,
                        "contentItems": [{"type": "inputText", "text": json.dumps(result)}]}})
                    if tools.calls >= 64:
                        raise RuntimeError("tool call budget reached")
                elif method == "thread/tokenUsage/updated":
                    if params.get("threadId") != thread_id:
                        raise RuntimeError("token usage thread mismatch")
                    tokens = params["tokenUsage"]["total"]["totalTokens"]
                    if type(tokens) is not int or tokens < 0 or tokens > 32000:
                        raise RuntimeError("token usage budget reached")
                    tools.usage = {"provider": "codex_subscription_operator", "model": self.model,
                                   "total_tokens": tokens}
                elif method in {"item/started", "item/completed"}:
                    if params.get("item", {}).get("type") not in {
                        "agentMessage", "reasoning", "dynamicToolCall", "userMessage"
                    }:
                        self.stage = "UNEXPECTED_ACTION"
                        raise RuntimeError("unexpected built-in action")
                elif method == "turn/completed":
                    self.stage = "TURN_COMPLETION"
                    if (params.get("threadId") != thread_id
                            or params.get("turn", {}).get("id") != turn_id
                            or params["turn"].get("status") != "completed"
                            or not tools.usage or tools.proposal is None):
                        raise RuntimeError("incomplete account proposal")
                    tools.record("provider", "COMPLETED", "PROPOSAL_COLLECTED")
                    return
                elif method == "error":
                    self.stage = "MODEL_ERROR"
                    error = params.get("error", {})
                    info = error.get("codexErrorInfo")
                    category = info if isinstance(info, str) else next(iter(info or {}), "unknown")
                    allowed = {"badRequest", "unauthorized", "usageLimitExceeded", "rateLimitExceeded",
                               "responseStreamConnectionFailed", "responseStreamDisconnected",
                               "responseTooManyFailedAttempts", "httpConnectionFailed", "other"}
                    category = category if category in allowed else "unknown"
                    status = info.get(category, {}).get("httpStatusCode") if isinstance(info, dict) else None
                    tools.record("provider", "STOPPED", "ACCOUNT_MODEL_ERROR", {
                        "category": category, "http_status": status if type(status) is int else None})
                    raise RuntimeError("account model failure")


async def run(args):
    if args.output.exists() or not args.codex.is_absolute():
        raise ValueError("new output and absolute Codex binary required")
    bundle = FailureBundle.model_validate(bounded_json(args.bundle, 16384))
    manifest = SnapshotManifest.model_validate(bounded_json(args.manifest, 32768))
    coordinator = Coordinator(bundle, Snapshot(args.snapshot.resolve(), manifest))
    with tempfile.TemporaryDirectory(prefix="bravo-account-") as directory:
        runtime = Path(directory)
        (runtime / "config.toml").write_text(configuration())
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": directory,
               "CODEX_HOME": directory, "TMPDIR": directory}
        version = await asyncio.create_subprocess_exec(str(args.codex), "--version", env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await version.communicate()
        if version.returncode or stdout.decode().strip() != CODEX_VERSION:
            raise RuntimeError("unverified Codex version")
        process = await asyncio.create_subprocess_exec(str(args.codex), "app-server",
            "--strict-config", "--stdio", cwd=runtime, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1048577)
        session = AccountSession(process, runtime)
        try:
            async with asyncio.timeout(660):
                await session.login()
            result = await coordinator.propose(AccountSmokeProvider(session, args.model))
            if (runtime / "auth.json").exists():
                raise RuntimeError("unexpected persisted credentials")
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
            print(json.dumps({"state": result["state"], "credentials_persisted":
                              (runtime / "auth.json").exists()}), flush=True)
            return 0 if result["state"] == "PROPOSAL_READY" else 2
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("codex", "bundle", "manifest", "snapshot", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", args.model):
        parser.error("invalid model identifier")
    try:
        return asyncio.run(run(args))
    except Exception:
        print('{"state":"NEEDS_HUMAN","reason":"ACCOUNT_OR_PROTOCOL_FAILURE"}', flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
