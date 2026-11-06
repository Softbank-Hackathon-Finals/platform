"""Test-only local T10 fixture. Never mount this module in a deployed service."""

import tempfile
from pathlib import Path

import uvicorn
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore
from expanded_fixture import setup_expanded
from fastapi import HTTPException, Request
from registration_fixture import install
from test_app import setup
from test_change_app import setup_change
from test_network import install as install_network
from test_network import snapshot as network_snapshot
from test_reports import wire
from test_security import Samples

from bravo_console import ConsoleApp
from bravo_console.security import PlanSecurity

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="t21-browser-") as root:
        app, store, state, _ = setup(
            Path(root) / "state.db",
            origin="http://127.0.0.1:18121",
            workload_id="demo-chat",
            profile=("demo-gate", "1"),
        )
        install(app.api, store, state, Path(root))
        install_network(app, state)
        state["network_document"] = network_snapshot()
        state["network_document"]["sources"][0]["data"]["resources"][0]["name"] = (
            '<img src=x onerror="window.__networkAttack=1">'
        )
        app.controls = wire(app.api)
        # Synthetic findings, real T10 auth/plan binding and ConsoleApp route.
        app.security = PlanSecurity(
            app.api,
            Samples(),
            environments={"env-source": "source", "env-target": "target"},
            authorize=lambda *_: state["allowed"],
        )
        # Separate durable stores/real T10 APIs for the two response-loss cases.
        # Fixtures exist only in this local test entry point, never ConsoleApp.
        apps = {}
        # No workflow gate event is seeded for this case. The report is a
        # synthetic authenticated BLOCK; real T10 registration starts at v1.
        initial_store = PlanStore(Path(root) / "initial-controls.db")
        initial_store.register_plan(
            PlanSpec(
                "plan-initial-controls",
                "tenant-demo",
                "demo-chat",
                "env-source",
                "env-target",
                "a" * 64,
                "b" * 64,
                "demo-gate",
                "1",
            ),
            run_id="fixture-registration",
            occurred_at="2026-11-06T00:00:00Z",
        )
        initial_api = create_plan_app(
            initial_store,
            resolve_principal=lambda request: (
                Principal("fixture-reader", "tenant-demo")
                if request.cookies.get("fixture-session") == "test-only"
                else None
            ),
        )

        def initial_block(document):
            document["decision"] = "BLOCK"
            document["rows"][0].update(
                target_verdict="UNKNOWN", decision="BLOCK", reason="TARGET_UNKNOWN"
            )
            return document

        apps["plan-initial-controls"] = ConsoleApp(
            initial_api,
            public_origin="http://127.0.0.1:18121",
            controls=wire(initial_api, authority={"change": initial_block}),
        )
        for plan in ("plan-before", "plan-after", "plan-approve-loss"):
            case, _, counters, _ = setup(
                Path(root) / (plan + ".db"),
                origin="http://127.0.0.1:18121",
                plan_id=plan,
            )

            def stats_endpoint(values):
                async def stats(request: Request):
                    if request.cookies.get("fixture-session") != "test-only":
                        raise HTTPException(403)
                    return {key: values[key] for key in ("submitted", "reads")}

                return stats

            case.api.add_api_route(f"/v1/plans/{plan}/fixture-stats", stats_endpoint(counters))
            apps[plan] = case

        for plan in ("plan-change", "plan-change-loss", "plan-change-pre", "plan-change-version"):
            case, _, counters, _ = setup_change(
                Path(root) / (plan + ".db"),
                origin="http://127.0.0.1:18121",
                plan_id=plan,
            )
            case.api.add_api_route(f"/v1/plans/{plan}/fixture-stats", stats_endpoint(counters))
            apps[plan] = case

        for plan, phase in (
            ("plan-workspace-console", "AWAIT_APPROVAL"),
            ("plan-shift-console", "AWAIT_APPROVAL"),
            ("plan-failback-console", "WATCH"),
            ("plan-rehearsal-console", "REHEARSE"),
            ("plan-initial-console", "PROPOSE"),
            ("plan-uncertain-console", "REHEARSE"),
            ("plan-before-console", "REHEARSE"),
        ):
            case, _, counters, _ = setup_expanded(
                Path(root) / (plan + ".db"),
                origin="http://127.0.0.1:18121",
                plan_id=plan,
                phase=phase,
            )

            def expanded_stats(values):
                async def stats(request: Request):
                    if request.cookies.get("fixture-session") != "test-only":
                        raise HTTPException(403)
                    return {key: values[key] for key in ("initial", "starts", "failback_posts")}

                return stats

            case.api.add_api_route(f"/v1/plans/{plan}/fixture-stats", expanded_stats(counters))
            install_network(case, counters)
            apps[plan] = case

        async def dispatch(scope, receive, send):
            path = scope.get("path", "")
            selected = next(
                (
                    value
                    for key, value in apps.items()
                    if path == f"/v1/plans/{key}" or path.startswith(f"/v1/plans/{key}/")
                ),
                app,
            )
            await selected(scope, receive, send)

        uvicorn.run(dispatch, host="127.0.0.1", port=18121, log_level="warning")
