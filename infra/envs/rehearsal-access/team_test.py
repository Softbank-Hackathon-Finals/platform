#!/usr/bin/env python3
"""Run an operator test with personal authentication and real impersonation.

Uses an existing named-member grant; never creates RBAC or shares admin keys.
The authenticated proxy issues a short-lived member token. Test requests use
a separate noauth API proxy, without a nested port-forward.
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


def credential():
    token = os.environ.get("BRAVO_TEAM_DIRECT_TOKEN")
    expiry = os.environ.get("BRAVO_TEAM_DIRECT_EXPIRY")
    if not token or not expiry:
        raise RuntimeError(
            "No active credential; invoke this file as a command wrapper"
        )
    print(
        json.dumps(
            {
                "apiVersion": "client.authentication.k8s.io/v1",
                "kind": "ExecCredential",
                "status": {"token": token, "expirationTimestamp": expiry},
            }
        )
    )


def profile(path):
    value = json.loads(path.read_text())
    if set(value) != {
        "server",
        "direct_server",
        "cluster_uid",
        "access_namespace",
        "members",
    }:
        raise ValueError(
            "Expected server, direct_server, cluster_uid, access_namespace and members"
        )
    for key in ("server", "direct_server"):
        url = urlsplit(value[key])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError(
                "An HTTPS API proxy origin without credentials is required"
            )
    if value["server"].rstrip("/") == value["direct_server"].rstrip("/"):
        raise ValueError("Authentication and direct API proxies must be distinct")
    if str(UUID(value["cluster_uid"])) != value["cluster_uid"]:
        raise ValueError("A canonical cluster UID is required")
    import re

    names = [value["access_namespace"], *value["members"].values()]
    if not value["members"] or any(
        not isinstance(n, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", n)
        for n in names
    ):
        raise ValueError(
            "Explicit member ServiceAccount names and namespace are required"
        )
    return value


def config(server, user=None, ca=None):
    cluster = {"server": server}
    if ca:
        cluster.update(
            {
                "certificate-authority-data": base64.b64encode(ca.encode()).decode(),
                "tls-server-name": "kubernetes.default.svc",
            }
        )
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "target", "cluster": cluster}],
        "contexts": [
            {"name": "target", "context": {"cluster": "target", "user": "test"}}
        ],
        "current-context": "target",
        "users": [{"name": "test", "user": user or {"token": "tailscale-auth-proxy"}}],
    }


def stop_group(process):
    """Stop our command and its descendants, including after the leader exits."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def supervise(command, env, deadline):
    if time.monotonic() >= deadline:
        raise RuntimeError("Credential expired; test was not started")
    child = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Ten-minute window ended; test stopped without replay. Inspect its resources before retrying"
                )
            result = child.poll()
            if result is not None:
                return result
            time.sleep(0.1)
    finally:
        stop_group(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", type=Path, default=Path(__file__).with_name("team-test.json")
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="New private JSON file for connection diagnostics; no credentials",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Supply a command after --")
    if os.name != "posix":
        parser.error("Use macOS, Linux or WSL")
    if args.diagnostics and args.diagnostics.exists():
        parser.error("Diagnostics file already exists; choose a new path")
    settings = profile(args.profile)
    os.umask(0o077)
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "BRAVO_TEAM_DIRECT_TOKEN",
        "BRAVO_TEAM_DIRECT_EXPIRY",
    ):
        env.pop(key, None)
    diagnostic = {
        "transport": "direct-noauth-api",
        "port_forward": False,
        "replayed": False,
    }
    with tempfile.TemporaryDirectory(prefix="bravo-team-test-") as tmp:
        root = Path(tmp)
        bootstrap = root / "tailscale.json"
        bootstrap.write_text(json.dumps(config(settings["server"])))
        base = ["kubectl", "--kubeconfig=" + str(bootstrap)]

        def capture(arguments):
            result = subprocess.run(
                base + ["--request-timeout=20s"] + arguments,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            return result.stdout

        actual = json.loads(capture(["auth", "whoami", "-o", "json"]))["status"][
            "userInfo"
        ]["username"]
        if actual not in settings["members"]:
            raise RuntimeError("This identity is not a named member")
        if (
            capture(["get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"])
            != settings["cluster_uid"]
        ):
            raise RuntimeError("Unexpected destination cluster")
        alias, namespace = settings["members"][actual], settings["access_namespace"]
        subject = "system:serviceaccount:" + namespace + ":" + alias
        token = capture(
            ["create", "token", alias, "-n", namespace, "--duration=10m"]
        ).strip()
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        if payload["sub"] != subject or not (
            time.time() < payload["exp"] <= time.time() + 660
        ):
            raise RuntimeError("Unexpected token subject or lifetime")
        deadline = time.monotonic() + payload["exp"] - time.time() - 5
        env["BRAVO_TEAM_DIRECT_TOKEN"] = token
        env["BRAVO_TEAM_DIRECT_EXPIRY"] = datetime.fromtimestamp(
            payload["exp"], UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        plugin = {
            "apiVersion": "client.authentication.k8s.io/v1",
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "credential"],
            "interactiveMode": "Never",
        }
        direct = root / "direct.json"
        direct.write_text(
            json.dumps(config(settings["direct_server"], {"exec": plugin}))
        )
        env["KUBECONFIG"] = str(direct)
        try:

            def direct_read(arguments):
                check = subprocess.run(
                    ["kubectl", "--request-timeout=15s"] + arguments,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=25,
                )
                if check.returncode:
                    raise RuntimeError(check.stderr.strip())
                return check.stdout

            identity = json.loads(direct_read(["auth", "whoami", "-o", "json"]))
            if identity["status"]["userInfo"]["username"] != subject:
                raise RuntimeError(
                    "Direct API identity mismatch; expected a noauth proxy"
                )
            if (
                direct_read(
                    ["get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}"]
                )
                != settings["cluster_uid"]
            ):
                raise RuntimeError("Unexpected direct API destination cluster")
            diagnostic.update(
                {
                    "subject": subject,
                    "cluster_uid": settings["cluster_uid"],
                    "expires": env["BRAVO_TEAM_DIRECT_EXPIRY"],
                }
            )
            print(
                "Bravo direct API: "
                + subject
                + " / expires "
                + env["BRAVO_TEAM_DIRECT_EXPIRY"],
                file=sys.stderr,
            )
            result = supervise(command, env, deadline)
            diagnostic["command_exit"] = result
            return result
        finally:
            if args.diagnostics:
                with args.diagnostics.open("x") as stream:
                    json.dump(diagnostic, stream, indent=2)


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if sys.argv[1:] == ["credential"]:
            credential()
            sys.exit(0)
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print("bravo-team-test: " + str(error), file=sys.stderr)
        sys.exit(1)
