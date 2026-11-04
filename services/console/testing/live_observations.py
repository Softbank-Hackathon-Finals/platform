"""Local T21 projection of an actual T37 HTTP endpoint; fixture plan and identity.

Explicit opt-in input on stdin keeps the ephemeral query token out of argv and logs.
No approval or executor is mounted. Not a production authentication recipe.
"""

import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore

from bravo_console import ConsoleApp
from bravo_console.observations import PlanObservations, T37Client


async def browser_projection(api, observations, cookie, phase, output):
    """The browser talks to the same real T21/T37 path; identity remains a fixture."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    # Bind the allowed Host to this actual port; never relax the host check for tests.
    app = ConsoleApp(api, public_origin=origin, observations=observations)
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="error", access_log=False, timeout_graceful_shutdown=2
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    process = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("local_browser_server_not_started")
                await asyncio.sleep(0.02)
        env = {k: v for k, v in os.environ.items() if not k.startswith(
            ("AWS_", "GH_", "GITHUB_", "OPENAI_", "ANTHROPIC_", "API_TOKEN")
        )}
        env.update(BRAVO_BROWSER_ORIGIN=origin, BRAVO_BROWSER_COOKIE=cookie,
                   BRAVO_BROWSER_PHASE=phase, BRAVO_BROWSER_OUTPUT=str(output))
        process = await asyncio.create_subprocess_exec(
            "node", "node_modules/@playwright/test/cli.js", "test",
            "--config", "testing/live_observations.config.js",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(70):
            stdout, stderr = await process.communicate()
        if process.returncode or len(stdout) + len(stderr) > 65536:
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
            detail = (stdout + stderr)[:65536].decode("utf-8", errors="replace")
            (output / "qualification-error.log").write_text(detail.replace(cookie, "<fixture-cookie>"))
            raise RuntimeError("live_observation_browser_check_failed")
        return {"projects": ["chromium", "webkit"], "status": "PASS"}
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)
        listener.close()


async def qualify(config):
    origin = config["origin"]
    parts = urlsplit(origin)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise ValueError("owned_loopback_test_endpoint_required")
    if config["phase"] not in ("collecting", "target-stopped"):
        raise ValueError("explicit_phase_required")
    cookie = secrets.token_hex(24)
    permission = {"allowed": True}
    client = T37Client(origin, config["token"])
    try:
        with tempfile.TemporaryDirectory(prefix="t21-live-observations-") as temp:
            store = PlanStore(Path(temp) / "state.db")
            plan = PlanSpec(
                "local-observation-validation",
                "local-validation",
                "demo-chat",
                "registered-aws",
                "registered-onprem",
                "a" * 64,
                "b" * 64,
            )
            store.register_plan(
                plan, run_id="test-registration", occurred_at=datetime.now(UTC).isoformat()
            )

            def principal(request):
                if request.cookies.get("local-validation") == cookie:
                    return Principal("test-operator", "local-validation")
                return None

            api = create_plan_app(store, resolve_principal=principal)
            observations = PlanObservations(
                api,
                client,
                environments={"registered-aws": "aws", "registered-onprem": "onprem"},
                authorize=lambda scope, current: permission["allowed"],
            )
            app = ConsoleApp(api, public_origin="http://127.0.0.1", observations=observations)
            path = "/v1/plans/local-observation-validation/observations"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as http:
                checks = {"unauthenticated_query_denied": (await http.get(path)).status_code == 403}
                http.cookies.set("local-validation", cookie)
                response = await http.get(path)
                response.raise_for_status()
                document = response.json()
                rows = {r["environment"]: r for r in document["environments"]}
                checks["fixed_environment_pair"] = set(rows) == {"aws", "onprem"}
                checks["no_gate_authority"] = document["for_gate_decisions"] is False
                checks["source_observed"] = rows["aws"]["state"] == "OK"
                if config["phase"] == "collecting":
                    checks["target_observed"] = rows["onprem"]["state"] == "OK"
                else:
                    checks["target_stop_is_unknown"] = rows["onprem"]["state"] == "UNOBSERVABLE"
                    checks["target_stop_has_no_invented_values"] = all(
                        rows["onprem"][field] is None
                        for field in ("requests_per_second", "error_ratio", "p95_seconds")
                    )
                checks["token_not_returned"] = config["token"] not in response.text
                permission["allowed"] = False
                revoked = await http.get(path)
                checks["separate_environment_permission_required"] = (
                    revoked.status_code == 403 and "environments" not in revoked.json()
                )
                http.cookies.clear()
                checks["session_revoked_query_denied"] = (await http.get(path)).status_code == 403
                browsers = None
                if config.get("browser_output"):
                    permission["allowed"] = True
                    browsers = await browser_projection(
                        api, observations, cookie, config["phase"], Path(config["browser_output"])
                    )
                    checks["actual_browser_projection"] = browsers["status"] == "PASS"
                return {
                    "scope": "actual T37 HTTP and T21 projection; local fixture identity/plan; no execution authority",
                    "phase": config["phase"],
                    "checks": checks,
                    "status": "PASS" if all(checks.values()) else "FAIL",
                    "observations": document,
                    "browsers": browsers,
                }
    finally:
        await client.aclose()


if __name__ == "__main__":
    os.umask(0o077)
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise SystemExit("bounded_test_configuration_required")
    result = asyncio.run(qualify(json.loads(raw)))
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
