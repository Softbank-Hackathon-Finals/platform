"""Local-only SOVA MCP with SYNTHETIC data for UI work (never a platform deployment).

    uv run python tests/dev_server.py            # http://127.0.0.1:8765/mcp, bearer "dev-local"

Same T10 SQLite PlanStore/Registry as the tests; states and metrics are made up. The desktop
console shows a "로컬 시험 데이터" banner for loopback MCP URLs.
"""

import math
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from test_server import APP, TENANT, Probe, principal  # noqa: E402

from bravo_platform_events.plans import PlanStore  # noqa: E402
from bravo_platform_events.registration import Registry  # noqa: E402
from bravo_platform_events.registration_probe import EnvironmentRef, SourceRelease  # noqa: E402
from sova_mcp.backend import SovaBackend, Upstream  # noqa: E402
from sova_mcp.server import create_app  # noqa: E402

FLOW = ["COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE", "AWAIT_APPROVAL", "SHIFT", "CUTOVER", "WATCH"]


def seed(store, registry, actor, root):
    def reg(kind, body, key):
        return registry.register(actor, kind, body, key)[0]

    reg("environment", dict(name="demo-chat AWS EKS", kind="aws", roles=["source"], connection_ref="source-reader"), "e1")
    reg("environment", dict(name="온프렘 RKE2", kind="onprem", roles=["target"], connection_ref="target-reader"), "e2")
    app = reg("workload", APP | {"name": "demo-chat"}, "w1")
    t0 = datetime.now(UTC) - timedelta(minutes=48)
    for n, stop in (("p1", "AWAIT_APPROVAL"), ("p2", "WATCH")):
        plan = reg("plan", dict(workload_id=app["workload_id"], target_environment_id="env-dst",
                                profile_id="full", profile_version="1"), n)
        pid = plan["plan_id"]
        steps = FLOW[: FLOW.index(stop) + 1]
        for i, (a, b) in enumerate(zip(steps, steps[1:], strict=False)):
            at = (t0 + timedelta(minutes=4 * i)).isoformat()
            with store._transaction(write=True, plan_id=pid) as c:  # harness acts as the state owner
                store._append_in_transaction(c, plan_id=pid, run_id="dev", event_type="state.changed", producer_key=f"s{i}",
                                             occurred_at=at, data={"from": a, "to": b}, state_owner=True)
            if b == "AWAIT_APPROVAL":
                store.append(plan_id=pid, run_id="dev", event_type="gate.completed", producer_key="g", occurred_at=at,
                             data={"gate_id": "demo-gate-1", "evidence_ref": "ev-dev", "decision": "PASS",
                                   "context_digest": "c" * 64})
            if b in ("SHIFT", "CUTOVER", "WATCH"):
                store.append(plan_id=pid, run_id="dev", event_type="execution.updated", producer_key=f"x{i}", occurred_at=at,
                             data={"execution_id": "exec-dev", "step": b.lower(), "status": "succeeded", "elapsed_ms": 1800 + 900 * i})
        with store._transaction(write=True, plan_id=pid) as c:
            c.execute("UPDATE plans SET state=?, version=? WHERE plan_id=?", (stop, len(steps), pid))


def metrics(request: httpx.Request):
    t = time.time() / 20
    env = request.url.path.split("/")[-2]
    base = 38 if env == "source" else 14
    return httpx.Response(200, json={
        "queried_at": datetime.now(UTC).isoformat(), "environment": env, "window": "1m", "state": "OK",
        "last_received_seconds_ago": 4, "requests_per_second": round(base + 6 * math.sin(t), 2),
        "error_ratio": round(abs(0.004 * math.sin(t * 1.7)), 4), "p95_seconds": round(0.12 + 0.03 * math.cos(t), 3)})


def main():
    root = Path(tempfile.mkdtemp(prefix="sova-dev-"))
    store = PlanStore(root / "plans.db")
    from dataclasses import replace
    src = EnvironmentRef(TENANT, "env-src", "aws", "app-src", "db", "src-id", "https://source.invalid", root / "ca", lambda: "dev")
    dst = replace(src, environment_id="env-dst", kind="onprem", namespace="app-dst", database_uid="dst-id")
    release = SourceRelease(TENANT, "env-src", APP["repo_url"], root, "app", "compose.yaml", "a" * 40, "chat", "chat",
                            "repo/chat@sha256:" + "b" * 64, ("sha256:" + "b" * 64,), "chat", "rs-uid")
    registry = Registry(store, environments={(TENANT, "source-reader"): src, (TENANT, "target-reader"): dst},
                        releases=[release], can_register=lambda _a: True, probe=Probe(),
                        evaluation_profiles=[dict(tenant_id=TENANT, profile_id="full", profile_version="1")])
    seed(store, registry, principal("dev"), root)
    (root / "token").write_text("dev")
    obs = Upstream("https://obs.invalid", root / "token", httpx.MockTransport(metrics))
    backend = SovaBackend(store, registry, "sova-console://open",
                          {TENANT: {"env-src": {"observability": "source"}, "env-dst": {"observability": "target"}}}, obs, None)
    app = create_app(backend, lambda r: principal("dev") if r.headers.get("authorization") == "Bearer dev-local" else None,
                     resource_url="http://127.0.0.1:8765/mcp", allowed_hosts=["127.0.0.1:*"])
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
