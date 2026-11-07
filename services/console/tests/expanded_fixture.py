"""Synthetic deployment bindings over the actual T10/T11/T24 implementations."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_platform_events.cutover import CutoverService, _key, _put
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.executor import ExecutionRead, ExecutionSnapshot, ExecutionStep
from bravo_platform_events.failback import CurrentFailback, FailbackService
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.rehearsal import RehearsalService, RehearsalTask
from bravo_platform_events.workflow_stages import ShiftApprovalService
from starlette.requests import Request
from test_app import ORIGIN, setup

from bravo_console import ConsoleApp
from bravo_console.audit import RegisteredAuditSnapshot, t24_reader
from bravo_console.operation_reads import T10Operations
from bravo_console.workspace_data import (
    AuditPanel,
    PlanWorkspace,
    RegisteredPanel,
    RegisteredPanelSource,
    T37Details,
)

SERVICES = Path(__file__).resolve().parents[2]


def setup_expanded(path, *, origin=ORIGIN, plan_id="plan-expanded", phase="AWAIT_APPROVAL"):
    _, store, state, cutover_binding = setup(
        path, origin=origin, plan_id=plan_id, profile=("demo-gate", "1")
    )
    now = datetime.now(UTC)
    expires = (now + timedelta(minutes=10)).isoformat()
    with store._transaction(write=True, plan_id=plan_id) as c:
        c.execute("UPDATE plans SET state=?,version=3 WHERE plan_id=?", (phase, plan_id))
        store._append_in_transaction(
            c,
            plan_id=plan_id,
            run_id="workspace-fixture",
            event_type="state.changed",
            data={"from": "SHIFT", "to": phase},
            occurred_at=now.isoformat(),
            producer_key="expanded-state",
            state_owner=True,
        )
        _put(c, plan_id, "workflow:2", {}, {"result": {"fixture": "synthetic-checkpoint"}})
    state.update(
        actor="fixture-operator",
        grants=True,
        starts=0,
        initial=0,
        source_allowed=True,
        failback_posts=0,
    )

    def resolve(request):
        if state["allowed"] and request.cookies.get("fixture-session") == "test-only":
            return Principal(state["actor"], "tenant-demo")
        return None

    def check(_c, _principal, _plan):
        from bravo_platform_events.store import EventError

        if not state["grants"]:
            raise EventError("forbidden")

    class Executor:
        def submit(self, *_a, **_k):
            state["failback_posts"] += 1
            return ExecutionRead("UNKNOWN", "fixture-response-loss")

        def read(self, *_a, **_k):
            return ExecutionRead("UNKNOWN", "fixture-read")

    class RehearsalHost:
        def task(self, _c, _plan, task_id):
            if task_id != "task-registered":
                raise ValueError("unknown_task")
            return RehearsalTask("1" * 40, "a" * 64)

        def submit(self, admission):
            state["starts"] += 1

        def observe(self, admission):
            return None

    base = CutoverService(
        store, Executor(), bindings={"env-source": "source", "env-target": "target"}
    )
    shift = ShiftApprovalService(store, check_approve=check)
    rehearsal = RehearsalService(store, host=RehearsalHost(), check_start=check)
    binding = {
        "plan_id": plan_id,
        "plan_digest": store.snapshot(plan_id)["plan_digest"],
        "source_environment_id": "env-target",
        "target_environment_id": "env-source",
        "cutover_execution_id": "cutover-execution",
        "cutover_epoch": 7,
        "integrity_receipt_id": "integrity-fixture",
        "expires_at": expires,
    }
    command = {
        "plan_id": plan_id,
        "epoch": 8,
        "source": {"environment_id": "env-target", "connection_ref": "target"},
        "target": {"environment_id": "env-source", "connection_ref": "source"},
        "write_pause_budget_ms": 30000,
        "deadline": expires,
    }
    if phase == "WATCH":
        with store._transaction(write=True, plan_id=plan_id) as c:
            snap = ExecutionSnapshot(
                "cutover-execution",
                plan_id,
                7,
                "DONE",
                True,
                (ExecutionStep("compare", "succeeded", 1),),
                now.isoformat(),
                now.isoformat(),
            )
            _put(
                c,
                plan_id,
                _key("command", "cutover-command"),
                {},
                {"command_id": "cutover-command", "command": {"epoch": 7}},
            )
            _put(
                c,
                plan_id,
                _key("observation", "cutover-command") + f"-{1:012d}",
                {},
                {"index": 1, "outcome": "OBSERVED", "reason": None, "snapshot": asdict(snap)},
            )
            _put(
                c,
                plan_id,
                "workflow:cutover",
                {},
                {
                    "receipt_id": "integrity-fixture",
                    "result": {
                        "run": {
                            "command_id": "cutover-command",
                            "epoch": 7,
                            "execution_id": "cutover-execution",
                        },
                        "matched": True,
                    },
                },
            )
    failback = FailbackService(
        store,
        Executor(),
        bindings={"env-source": "source", "env-target": "target"},
        authority=lambda *_a, **_kw: CurrentFailback(binding, command, "fixture-authority"),
        check_access=check,
    )
    api = create_cutover_app(
        base,
        resolve_principal=resolve,
        shift_service=shift,
        rehearsal_service=rehearsal,
        failback_service=failback,
    )

    def initial(_scope, plan, operation):
        state["initial"] += 1
        return {
            "status": "DONE",
            "request_id": "initial-fixture",
            "runtime_source_sha": "1" * 40,
            "proposal_digest": "a" * 64,
        }

    operations = T10Operations(
        api,
        store,
        resolve_principal=lambda scope: resolve(Request(scope)),
        authorize=lambda *_: state["grants"],
        shift=shift,
        failback=failback,
        rehearsal=rehearsal,
        rehearsal_tasks=lambda *_: ["task-registered"],
        initial=initial,
        check_initial=lambda *_: state["grants"],
    )
    snap = store.snapshot(plan_id)
    inventory = json.loads(
        (SERVICES / "target-recommend/tests/fixtures/t16-inventory-demo-chat.json").read_text()
    )
    inventory["app"]["id"], inventory["source"]["env_id"] = (
        snap["workload_id"],
        snap["source_environment_id"],
    )
    from bravo_inventory.report import _digest

    inventory["digest"] = _digest(
        {k: v for k, v in inventory.items() if k not in {"generated_at", "digest"}}
    )
    from bravo_recommend.recommend import recommend

    docs = {
        "inventory": inventory,
        "recommendations": recommend(inventory),
        "exit": {
            "app": snap["workload_id"],
            "candidate": snap["target_environment_id"],
            "entries": [
                {
                    "name": "object_store",
                    "category": "source_dependency",
                    "grade": "code_change",
                    "target_impl": "seaweedfs",
                    "status": "unverified",
                    "check_verdict": "NOT_RUN",
                }
            ],
            "drill_verdict": "NOT_RUN",
            "candidate_verdict": "needs_work",
            "inventory_digest": inventory["digest"],
        },
        "ai": {
            "workload_id": snap["workload_id"],
            "request_id": "request-fixture",
            "status": "PENDING",
            "changes": [
                {
                    "path": "app/storage.py",
                    "kind": "code_change",
                    "summary": "목적지 저장소 API 사용",
                }
            ],
        },
        "rehearsal": {
            "attempts": [{"rehearsal_id": "run-fixture", "attempt": 1, "status": "UNCONFIRMED"}]
        },
        "traffic": {
            "weights": {"source": 90, "target": 10},
            "write_pause_ms": 420,
            "observed_at": now.isoformat(),
            "receipt_id": "edge-fixture",
            "judgements": [
                {
                    "phase": "SHIFT",
                    "decision": "UNKNOWN",
                    "window_id": "window-fixture",
                    "profile_version": "fixture",
                    "evaluated_at": now.isoformat(),
                }
            ],
        },
    }

    def money(amount):
        return {"usd": amount, "krw_estimate": amount, "observed": amount is not None}

    docs["costs"] = {
        "scope": "account",
        "project": "bravo-platform",
        "metric": "UnblendedCost",
        "components": {"source": money("1.0"), "platform": money("2.0"), "target": money(None)},
        "project_unattributed": money("0"),
        "other_projects": money("3"),
        "account_total": money("6"),
        "period": {"start_inclusive": "2026-11-01", "end_exclusive": "2026-11-02"},
        "attribution_complete": False,
        "estimated": True,
        "fx": {
            "monthly_budget_krw": 500000,
            "krw_per_usd": 1343,
            "fx_date": "2026-11-02",
            "fx_source": "fixture",
        },
    }
    registered = {}
    for feature, doc in docs.items():
        raw = json.dumps(doc).encode()
        registered[feature] = RegisteredPanel(
            plan_id,
            snap["plan_digest"],
            snap["version"],
            feature,
            "1" * 40,
            hashlib.sha256(raw).hexdigest(),
            expires,
            raw,
        )
    sources = {
        feature: RegisteredPanelSource(
            feature,
            load=lambda _s, _p, f=feature: registered[f],
            authenticate=lambda *_: state["source_allowed"],
        )
        for feature in docs
    }

    from bravo_agent.contracts import FailureBundle, SnapshotManifest
    from bravo_agent.tools import PolicyDenied, Snapshot, Tools
    from bravo_audit.adapters import from_agent_tool_event
    from bravo_audit.ledger import RunLedger

    source = path.parent / (plan_id + "-code")
    source.mkdir(exist_ok=True)
    (source / "config.py").write_bytes(b"x = 1\n")
    bundle = FailureBundle(
        run_id="run-fixture",
        source_sha="1" * 40,
        failure_code="DB_CONNECTION_FAILED",
        summary="Fixture",
        evidence_digest="a" * 64,
    )
    tools = Tools(
        bundle,
        Snapshot(
            source,
            SnapshotManifest(
                source_sha="1" * 40,
                files={"config.py": hashlib.sha256(b"x = 1\n").hexdigest()},
                editable_paths=["config.py"],
            ),
        ),
        1,
    )
    try:
        tools.dispatch("rehearsal.exec", {"command": "aws s3 rb s3://fixture --force"})
    except PolicyDenied:
        pass
    p, run, audit_source, kind, origin_id, payload = from_agent_tool_event(
        tools.events[0], plan_id, 0
    )
    ledger = RunLedger(p, run)
    record, _ = ledger.append(audit_source, kind, payload, origin_id)
    audit = RegisteredAuditSnapshot(
        plan_id,
        snap["plan_digest"],
        run,
        1,
        record.sha256,
        json.dumps({"records": [record.as_dict()]}).encode(),
    )

    async def load_audit(*_):
        return audit

    async def auth_audit(*_):
        return state["source_allowed"]

    sources["audit"] = AuditPanel(t24_reader(api, load=load_audit, authenticate=auth_audit))
    import httpx

    from bravo_console.observations import PlanObservations, T37Client

    async def handle(request):
        name = request.url.path.split("/")[-2]
        route = request.url.path.split("/")[-1]
        d = {
            "queried_at": datetime.now(UTC).isoformat(),
            "environment": name,
            "state": "OK",
            "last_received_seconds_ago": 1,
        }
        if route == "http":
            d.update(window="1m", requests_per_second=12, error_ratio=0.01, p95_seconds=0.3)
        if route == "database":
            d.update(
                instances=[{"cluster": "app-db", "pod": "db-1", "role": "primary", "up": True}],
                replication_slots=[
                    {"cluster": "app-db", "slot": "replica", "lag_bytes": 23, "active": True}
                ],
            )
        if route == "pgbouncer":
            d.update(
                poolers=[
                    {
                        "pooler": "pool-1",
                        "up": True,
                        "databases": [
                            {
                                "name": "app",
                                "paused": False,
                                "clients_active": 2,
                                "clients_waiting": 1,
                                "servers_active": 2,
                            }
                        ],
                    }
                ]
            )
        if route == "alerts":
            d = {
                "queried_at": datetime.now(UTC).isoformat(),
                "alerts": [
                    {
                        "environment": "source",
                        "alertname": "SovaFixture",
                        "severity": "warning",
                        "state": "firing",
                    }
                ],
            }
        return httpx.Response(200, json=d)

    fixed = T37Client("https://observability.example.invalid", "x" * 32)
    fixed.client = httpx.AsyncClient(
        base_url="https://observability.example.invalid", transport=httpx.MockTransport(handle)
    )
    sources["monitoring"] = T37Details(
        fixed, environments={"env-source": "source", "env-target": "target"}
    )
    workspace = PlanWorkspace(
        api, sources=sources, operations=operations, authorize=lambda *_: state["source_allowed"]
    )
    from test_reports import wire
    from test_security import Samples

    from bravo_console.security import PlanSecurity

    mapping = {"env-source": "source", "env-target": "target"}
    app = ConsoleApp(
        api,
        public_origin=origin,
        workspace=workspace,
        observations=PlanObservations(
            api, fixed, environments=mapping, authorize=lambda *_: state["source_allowed"]
        ),
        controls=wire(api),
        security=PlanSecurity(
            api, Samples(), environments=mapping, authorize=lambda *_: state["source_allowed"]
        ),
    )
    return app, store, state, registered
