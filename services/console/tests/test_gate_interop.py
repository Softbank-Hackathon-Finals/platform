"""T14 producer → T19 evaluator → T10 plan → T21 HTTP projection.

Collector observations and host identity/authority are explicit local fixtures.
This test does not attest a cloud control or authorize a real cutover.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console import ConsoleApp
from bravo_console.reports import PlanControlReport, RegisteredGateDocument


@pytest.mark.parametrize(
    "mode,decision,reason",
    [
        ("healthy", "PASS", "BOTH_PASS"),
        ("public-target", "BLOCK", "TARGET_FAIL"),
        ("missing-target", "BLOCK", "TARGET_UNKNOWN"),
        ("missing-source", "BLOCK", "SOURCE_UNKNOWN"),
        ("expired", "BLOCK", "EVIDENCE_EXPIRED"),
    ],
)
def test_real_gate_decision_reaches_console_without_granting_execution(
    tmp_path, monkeypatch, mode, decision, reason
):
    # Repository integration test: consume the actual sibling implementations,
    # not copied judgement logic or canned GateComparison documents.
    services = Path(__file__).resolve().parents[2]
    for sibling in ("control-runner", "control-gate"):
        monkeypatch.syspath_prepend(str(services / sibling / "src"))
    from bravo_controls.runner import Catalog, Environment, Observation, Runner
    from bravo_gate.contract import T5Contract, canonical
    from bravo_gate.models import GateContext, RunSnapshot
    from bravo_gate.service import GateService

    wrapped, store, state, _ = setup(
        tmp_path / "gate.db", workload_id="demo-chat", profile=("demo-gate", "1")
    )
    plan = store.snapshot("plan-demo")
    contract = T5Contract()
    catalog = Catalog(contract.catalog, contract.schema)
    specs = catalog.profile("demo-gate", "1")
    now = datetime.now(UTC) - timedelta(seconds=1)
    collected = now - timedelta(days=2) if mode == "expired" else now

    def observe(context, spec):
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            spec.expected,
            "pass" if spec.positive_control == "required" else "not_applicable",
            collected,
            "synthetic-interop-probe",
            "1",
            "sha256:" + "a" * 64,
        )

    runs = {}
    for side in ("source", "target"):
        probes = {spec.test_id: observe for spec in specs}
        if mode == "missing-" + side:
            probes.pop("IAM-01.1")
        if side == "target" and mode == "public-target":
            probes["IAM-01.1"] = lambda context, spec: replace(
                observe(context, spec), observed="allow"
            )
        document = Runner(catalog, probes, clock=lambda: collected).run(
            profile_id="demo-gate",
            profile_version="1",
            app="demo-chat",
            environment=Environment("env-" + side, side, "fixture/" + side),
        )
        runs[side] = RunSnapshot(side + "-run", canonical(document))
    context = GateContext(
        "tenant-demo", "demo-chat", "gate-real-evaluator", plan["plan_digest"],
        "demo-gate", "1", contract.catalog_digest,
        "env-source", "fixture/source", "env-target", "fixture/target",
        runs["source"].ref, runs["target"].ref,
        runs["source"].fingerprint, runs["target"].fingerprint,
    )
    reports = {}
    gate = GateService(
        contract=contract,
        context_provider=lambda *_: context,
        run_provider=lambda _, __, side: runs[side],
        approval_provider=lambda *_: None,
        authenticate_context=lambda *_: True,
        authenticate_run=lambda *_: True,
        authenticate_approval=lambda *_: False,
        report_provider=lambda _, ctx: reports.get(ctx.gate_id),
        authenticate_report=lambda *_: True,
        authorize=lambda *_: True,
        clock=lambda: now,
    )
    document = gate.evaluate("fixture-operator", "fixture-context")
    assert document["decision"] == decision
    raw = canonical(document)
    reports[context.gate_id] = RunSnapshot(context.gate_id, raw)
    store.append(
        plan_id=plan["plan_id"], run_id="real-gate-evaluator",
        event_type="gate.completed",
        data={"gate_id": context.gate_id, "context_digest": context.fingerprint,
              "decision": decision, "evidence_ref": "fixture-retained-report"},
        occurred_at=now.isoformat(), producer_key="real-gate-evaluator",
    )
    registered = RegisteredGateDocument(plan["plan_id"], plan["plan_digest"], raw)
    authority = {"trusted": True}

    async def load(_scope, _plan):
        return registered

    async def authenticate(_scope, _plan, value):
        return authority["trusted"] and value == registered

    app = ConsoleApp(
        wrapped.api, public_origin=ORIGIN,
        controls=PlanControlReport(wrapped.api, load=load, authenticate=authenticate),
    )
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as client:
        response = client.get("/v1/plans/plan-demo/controls")
        assert response.status_code == 200, response.text
        visible = response.json()
        assert visible["decision"] == decision
        assert visible["context_digest"] == context.fingerprint
        assert visible["authorizes_cutover"] is False
        row = next(row for row in visible["rows"] if row["test_id"] == "IAM-01.1")
        assert row["reason"] == reason
        assert len(visible["rows"]) == len(specs)
        assert state["submitted"] == 0
        # The same valid gate bytes become unavailable after issuer revocation.
        authority["trusted"] = False
        revoked = client.get("/v1/plans/plan-demo/controls")
        assert revoked.status_code == 503 and "rows" not in revoked.json()
        assert state["submitted"] == 0
