"""Local T21 projection of an actual T37 HTTP endpoint; fixture plan and identity.

Explicit opt-in input on stdin keeps the ephemeral query token out of argv and logs.
No approval or executor is mounted. Not a production authentication recipe.
"""

import asyncio
import json
import secrets
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore

from bravo_console import ConsoleApp
from bravo_console.observations import PlanObservations, T37Client


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
                return {
                    "scope": "actual T37 HTTP and T21 projection; local fixture identity/plan; no execution authority",
                    "phase": config["phase"],
                    "checks": checks,
                    "status": "PASS" if all(checks.values()) else "FAIL",
                    "observations": document,
                }
    finally:
        await client.aclose()


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise SystemExit("bounded_test_configuration_required")
    result = asyncio.run(qualify(json.loads(raw)))
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
