"""Explicit administrator enrollment and a single-VM subscription credential broker."""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import boto3

from .subscription_auth import AuthFailure, LoginAttempt, OAuth, cleared_record
from .subscription_store import (
    SubscriptionStore,
    broker_lock,
    credential_policy,
    host_id,
    private_directory,
)


def login(store, directory, authorization_file=None):
    record, version = store.read()
    if record["state"] == "ACTIVE":
        raise AuthFailure("ALREADY_CONNECTED_REVOKE_BEFORE_LOGIN")
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Callback URLs contain one-time authorization codes.

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/auth/callback":
                self.send_error(404)
                return
            if "callback" in outcome or len(self.path) > 16384:
                self.send_error(400)
                return
            outcome["callback"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Return to Bravo's administrator command to check the connection result.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        attempt = LoginAttempt(f"http://127.0.0.1:{server.server_port}/auth/callback", host_id(directory),
                               record.get("client_id", "dynamic_agent_client"), record.get("subject"))
        url = attempt.authorization_url()
        if record["state"] == "PLAN_DISABLED":
            url += "&prompt=consent"
        if authorization_file:
            private_directory(authorization_file.parent)
            fd = os.open(authorization_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(url)
        else:
            webbrowser.open(url)
        print('{"state":"ADMIN_CONSENT_REQUIRED"}', flush=True)
        try:
            while "callback" not in outcome and time.time() < attempt.expires_at:
                server.handle_request()
            if "callback" not in outcome:
                raise AuthFailure("LOGIN_EXPIRED")
            # Activation happens only after signature, nonce, account, scope and version checks.
            credentials = OAuth().exchange(attempt, outcome["callback"], store.host_id)
            store.replace(credentials, version)
        finally:
            if authorization_file:
                authorization_file.unlink(missing_ok=True)
    print(json.dumps({"state": "ADMIN_SUBSCRIPTION_CONNECTED" if credentials["state"] == "ACTIVE"
                     else "PLAN_DISABLED"}))


def run_worker(store, directory, args):
    if host_id(directory) != store.host_id:
        raise AuthFailure("DESIGNATED_RUNTIME_HOST_REQUIRED")
    with broker_lock(directory):
        oauth = OAuth()
        token = store.access_token(oauth)
        if args.model not in oauth.models(token):
            raise AuthFailure("MODEL_NOT_AVAILABLE_FOR_ADMIN_ACCOUNT")
        return _isolated_worker("chatgpt-plan", {"ACCESS_TOKEN": token}, args)


def api_key(path: Path) -> str:
    """Anthropic API key file materialized for the broker user only (from platform OpenBao)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise AuthFailure("API_KEY_FILE_NOT_PRIVATE")
        key = os.read(fd, 4097).decode().strip()
    finally:
        os.close(fd)
    if re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,4000}", key) is None:
        raise AuthFailure("API_KEY_INVALID")
    return key


def run_claude_worker(key_file, directory, args):
    """Same isolated T11 worker with the Claude Agent SDK adapter and an API key.

    A claude.ai subscription login is not used here: Anthropic does not allow third-party
    products, including Agent SDK agents, to offer claude.ai login or rate limits.
    """
    with broker_lock(directory):
        return _isolated_worker("claude", {"ANTHROPIC_API_KEY": api_key(Path(key_file))}, args)


def _isolated_worker(provider, credential, args):
    with tempfile.TemporaryDirectory(prefix="bravo-subscription-") as temp:
        command = [sys.executable, "-m", "bravo_agent.cli", "analyze", "--provider", provider,
                   "--model", args.model]
        for name in ("bundle", "manifest", "snapshot", "output"):
            command += ["--" + name, str(getattr(args, name).absolute())]
        # Optional T16 input. Callers that build ``args`` themselves (T38 product bridge) omit it.
        if getattr(args, "inventory", None) is not None:
            command += ["--inventory", str(args.inventory.absolute())]
        # Optional T44 desired state of the target environment: enables infra.plan.
        if getattr(args, "iac_state", None) is not None:
            command += ["--iac-state", str(args.iac_state.absolute())]
        if getattr(args, "reasoning_effort", None) is not None:
            command += ["--reasoning-effort", args.reasoning_effort]
        if getattr(args, "sandbox_image", None) is not None:
            command += ["--sandbox-image", args.sandbox_image]
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": temp,
                       "TMPDIR": temp, "PYTHONDONTWRITEBYTECODE": "1", **credential,
                       **(getattr(args, "docker", None) or {})}
        # Inject only inference access. OS/metadata/egress isolation is a host obligation;
        # an environment allowlist alone is not an operating-system sandbox.
        process = subprocess.run(command, cwd=temp, env=environment, capture_output=True,
                                 timeout=210, text=True)
        state = "PROPOSAL_READY" if process.returncode == 0 else "NEEDS_HUMAN"
        print(json.dumps({"state": state, "provider": provider.replace("-", "_")}))
        return 0 if process.returncode == 0 else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init_host = commands.add_parser("init-host")
    init_host.add_argument("--state-dir", type=Path, required=True)
    init_secret = commands.add_parser("init-secret")
    init_secret.add_argument("--name", required=True)
    init_secret.add_argument("--region", required=True)
    init_secret.add_argument("--runtime-host-id", required=True)
    policy = commands.add_parser("policy")
    policy.add_argument("--secret-arn", required=True)
    for name in ("login", "status", "revoke", "models", "run"):
        command = commands.add_parser(name)
        command.add_argument("--secret-arn", required=True)
        command.add_argument("--runtime-host-id", required=True)
        if name != "status":
            command.add_argument("--state-dir", type=Path, required=True)
        if name == "login":
            command.add_argument("--authorization-file", type=Path)
        if name == "run":
            command.add_argument("--model", required=True)
            for item in ("bundle", "manifest", "snapshot", "output"):
                command.add_argument("--" + item, type=Path, required=True)
            command.add_argument("--inventory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "policy":
            print(json.dumps(credential_policy(args.secret_arn), indent=2))
            return 0
        if args.command == "init-host":
            print(json.dumps({"runtime_host_id": host_id(args.state_dir)}))
            return 0
        if args.command == "init-secret":
            if (not args.name.startswith("bravo-platform/agent-subscription/")
                    or not args.runtime_host_id.startswith("urn:uuid:")
                    or uuid.UUID(args.runtime_host_id).version != 4):
                raise AuthFailure("TEAM_SECRET_AND_HOST_REQUIRED")
            result = boto3.client("secretsmanager", region_name=args.region).create_secret(
                Name=args.name, Description="Bravo app-owned administrator subscription; no Codex cache",
                SecretString=json.dumps({"schema_version": 1, "state": "UNCONFIGURED",
                                         "runtime_host_id": args.runtime_host_id}),
                Tags=[{"Key": "project", "Value": "bravo-platform"},
                      {"Key": "component", "Value": "platform"}])
            print(json.dumps({"state": "UNCONFIGURED", "secret_arn": result["ARN"]}))
            return 0
        store = SubscriptionStore(args.secret_arn, args.runtime_host_id)
        if args.command == "status":
            record, _ = store.read()
            print(json.dumps({"state": record["state"], "email": record.get("email"),
                              "expires_at": record.get("expires_at"),
                              "subscription_scope_granted": "chatgpt.tokens.use.direct" in record.get("scopes", [])}))
        elif args.command == "login":
            with broker_lock(args.state_dir):
                login(store, args.state_dir, args.authorization_file)
        elif args.command == "revoke":
            if host_id(args.state_dir) != store.host_id:
                raise AuthFailure("DESIGNATED_RUNTIME_HOST_REQUIRED")
            with broker_lock(args.state_dir):
                record, version = store.read()
                if "refresh_token" in record:
                    OAuth().revoke(record)
                    store.replace(cleared_record(record), version)
                print('{"state":"SIGNED_OUT"}')
        elif args.command == "models":
            if host_id(args.state_dir) != store.host_id:
                raise AuthFailure("DESIGNATED_RUNTIME_HOST_REQUIRED")
            with broker_lock(args.state_dir):
                oauth = OAuth()
                print(json.dumps({"models": oauth.models(store.access_token(oauth))}))
        else:
            return run_worker(store, args.state_dir, args)
        return 0
    except AuthFailure as error:
        print(json.dumps({"state": "NEEDS_HUMAN", "reason": str(error)}))
        return 2
    except Exception:
        # SDK/HTTP exceptions can contain token bodies, account identities and resource addresses.
        print('{"state":"NEEDS_HUMAN","reason":"SUBSCRIPTION_AUTH_OR_STORE_FAILURE"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
