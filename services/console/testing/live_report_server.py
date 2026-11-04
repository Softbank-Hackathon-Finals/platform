"""Opt-in local T21 projection of an unchanged, externally produced T19 report.

Fixture identity/plan registration only. Not an authentication or hosting recipe.
The report's time/verdicts are never rewritten and no approval/executor is mounted.
"""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from bravo_platform_events.plan_api import Principal, create_plan_app
from bravo_platform_events.plans import PlanSpec, PlanStore

from bravo_console import ConsoleApp
from bravo_console.reports import PlanControlReport, RegisteredGateDocument


def main():
    path = Path(os.environ["BRAVO_TEST_GATE_REPORT"]).resolve()
    expected = os.environ["BRAVO_TEST_GATE_SHA256"]
    plan_digest = os.environ["BRAVO_TEST_PLAN_DIGEST"]
    raw = path.read_bytes()
    if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("explicit report fingerprint mismatch")
    doc = json.loads(raw)
    if doc["decision"] != "BLOCK" or doc["profile"] != {"id": "full", "version": "1"}:
        raise ValueError("this qualification expects an actual incomplete full-profile report")
    now = datetime.now(UTC)
    # Fixture plan binds the retained exact result, not an operational migration.
    plan = PlanSpec(
        "live-audit-validation",
        "local-validation",
        doc["app"],
        doc["source_environment_id"],
        doc["target_environment_id"],
        plan_digest,
        doc["context_digest"],
    )
    with tempfile.TemporaryDirectory(prefix="t21-live-report-") as temp:
        store = PlanStore(Path(temp) / "state.db")
        store.register_plan(plan, run_id="report-registration", occurred_at=now.isoformat())
        store.append(
            plan_id=plan.plan_id,
            run_id="report-registration",
            event_type="gate.completed",
            producer_key="retained-report",
            occurred_at=now.isoformat(),
            data={
                "gate_id": doc["gate_id"],
                "context_digest": doc["context_digest"],
                "decision": doc["decision"],
                "evidence_ref": "local-retained-report",
            },
        )

        def principal(request):
            if request.cookies.get("local-validation") == "browser-test-only":
                return Principal("test-operator", "local-validation")
            return None

        api = create_plan_app(store, resolve_principal=principal)

        async def load(scope, current):
            return RegisteredGateDocument(plan.plan_id, plan.plan_digest, raw)

        async def authenticate(scope, current, registered):
            # Explicit fixture authority over retained immutable bytes. Does not claim
            # to verify a production T19 issuer, approval, or still-live source evidence.
            return (
                current["plan_id"] == plan.plan_id
                and registered.document == raw
                and hashlib.sha256(registered.document).hexdigest() == expected
            )

        app = ConsoleApp(
            api,
            public_origin="http://127.0.0.1:18122",
            controls=PlanControlReport(api, load=load, authenticate=authenticate),
        )
        uvicorn.run(app, host="127.0.0.1", port=18122, log_level="warning")


if __name__ == "__main__":
    main()
