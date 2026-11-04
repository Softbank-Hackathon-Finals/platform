"""Test-only local T10 fixture. Never mount this module in a deployed service."""

import tempfile
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from registration_fixture import install
from test_app import setup
from test_reports import wire

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="t21-browser-") as root:
        app, store, state, _ = setup(
            Path(root) / "state.db", origin="http://127.0.0.1:18121", workload_id="demo-chat"
        )
        install(app.api, store, state, Path(root))
        app.controls = wire(app.api)
        # Separate durable stores/real T10 APIs for the two response-loss cases.
        # Fixtures exist only in this local test entry point, never ConsoleApp.
        apps = {}
        for plan in ("plan-before", "plan-after"):
            case, _, counters, _ = setup(
                Path(root) / (plan + ".db"),
                origin="http://127.0.0.1:18121", plan_id=plan,
            )

            def stats_endpoint(values):
                async def stats(request: Request):
                    if request.cookies.get("fixture-session") != "test-only":
                        raise HTTPException(403)
                    return {key: values[key] for key in ("submitted", "reads")}
                return stats

            case.api.add_api_route(f"/v1/plans/{plan}/fixture-stats", stats_endpoint(counters))
            apps[plan] = case

        async def dispatch(scope, receive, send):
            path = scope.get("path", "")
            selected = next((value for key, value in apps.items()
                             if path == f"/v1/plans/{key}" or path.startswith(f"/v1/plans/{key}/")), app)
            await selected(scope, receive, send)

        uvicorn.run(dispatch, host="127.0.0.1", port=18121, log_level="warning")
