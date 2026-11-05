"""Opt-in local T11→T24 FileSink→T21 browser qualification, never a host recipe."""

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.tools import PolicyDenied, Snapshot, Tools
from bravo_audit.adapters import from_agent_tool_event
from bravo_audit.ledger import RunLedger
from bravo_audit.sink import FileSink
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore

from bravo_console import ConsoleApp
from bravo_console.audit import RegisteredAuditSnapshot, t24_reader


def main():
    with tempfile.TemporaryDirectory(prefix="t21-audit-browser-") as directory:
        root = Path(directory)
        content = b"DB_HOST = 'localhost'\n"
        (root / "config.py").write_bytes(content)
        bundle = FailureBundle(
            run_id="run-browser",
            source_sha="1" * 40,
            failure_code="DB_CONNECTION_FAILED",
            summary="Destination DB unavailable",
            evidence_digest="a" * 64,
        )
        manifest = SnapshotManifest(
            source_sha=bundle.source_sha,
            files={"config.py": hashlib.sha256(content).hexdigest()},
            editable_paths=["config.py"],
        )
        tools = Tools(bundle, Snapshot(root, manifest), 1)
        try:
            tools.dispatch("rehearsal.exec", {"command": "aws s3 rb s3://test-attachments --force"})
        except PolicyDenied:
            pass
        assert tools.events[0]["outcome"] == "DENIED"
        plan, run, source, kind, origin, payload = from_agent_tool_event(
            tools.events[0], "plan-audit", 0
        )
        ledger = RunLedger(plan, run)
        record, created = ledger.append(source, kind, payload, origin)
        assert created
        sink = FileSink(root / "records")
        receipt = sink.put(record)
        stored = json.loads((sink.root / receipt.key).read_bytes())
        store = PlanStore(root / "plan.db")
        spec = PlanSpec(
            plan, "local-validation", "demo-chat", "env-source", "env-target", "a" * 64, "b" * 64
        )
        store.register_plan(spec, run_id="setup", occurred_at=datetime.now(UTC).isoformat())

        def principal(request):
            if request.cookies.get("local-validation") == "browser-test-only":
                return Principal("test-operator", "local-validation")
            return None

        api = create_plan_app(store, resolve_principal=principal)
        registered = RegisteredAuditSnapshot(
            plan,
            spec.plan_digest,
            run,
            1,
            record.sha256,
            json.dumps({"records": [stored]}).encode(),
        )

        async def load(scope, current):
            return registered

        async def authenticate(scope, current, value):
            return value is registered and current["plan_digest"] == spec.plan_digest

        app = ConsoleApp(
            api,
            public_origin="http://127.0.0.1:18124",
            audit=t24_reader(api, load=load, authenticate=authenticate),
        )
        uvicorn.run(app, host="127.0.0.1", port=18124, log_level="warning")


if __name__ == "__main__":
    main()
