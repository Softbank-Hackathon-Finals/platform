import json
from dataclasses import replace
from pathlib import Path

import pytest

from bravo_gate.contract import T5Contract, canonical, fingerprint, time
from bravo_gate.models import GateContext, RunSnapshot, StoredException
from bravo_gate.service import GateService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def contract():
    return T5Contract()


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def bundle(contract):
    source = load("examples/source-run.json")
    target = load("examples/target-run-after-fix.json")
    ctx = GateContext(
        "tenant-one",
        "demo-chat",
        "gate-0002",
        "a" * 64,
        "demo-gate",
        "1",
        contract.catalog_digest,
        "env-aws",
        "aws:eks/source",
        "env-onprem",
        "onprem:rke2/destination",
        "source-ref",
        "target-ref",
        fingerprint(source),
        fingerprint(target),
    )
    # Environment refs are registered actual identifiers, not inferred from IDs.
    ctx = replace(
        ctx,
        source_environment_ref=source["environment"]["ref"],
        target_environment_ref=target["environment"]["ref"],
    )
    now = time("2026-11-04T11:35:00Z")
    return ctx, source, target, now


@pytest.fixture
def service(contract, bundle):
    ctx, source, target, now = bundle
    state = {
        "context": ctx,
        "runs": {
            "source": RunSnapshot(ctx.source_ref, canonical(source)),
            "target": RunSnapshot(ctx.target_ref, canonical(target)),
        },
        "approvals": {},
        "reports": {},
        "allowed": True,
        "trusted": True,
        "now": now,
        "late": lambda: None,
    }

    def authorize(*_):
        return state["allowed"]

    def authenticate(*_):
        state["late"]()
        return state["trusted"]

    app = GateService(
        contract=contract,
        context_provider=lambda *_: state["context"],
        run_provider=lambda _, __, side: state["runs"][side],
        approval_provider=lambda _, __, id: state["approvals"].get(id),
        authenticate_context=authenticate,
        authenticate_run=authenticate,
        authenticate_approval=authenticate,
        report_provider=lambda _, ctx: state["reports"].get(ctx.gate_id),
        authenticate_report=authenticate,
        authorize=authorize,
        clock=lambda: state["now"],
    )
    # Test-only persistence adapter. Production evaluate() does not publish or
    # trust client documents; the host must persist its own evaluator result.
    evaluate = app.evaluate
    def evaluate_and_store(*args, **kwargs):
        report = evaluate(*args, **kwargs)
        state["reports"][report["gate_id"]] = RunSnapshot(report["gate_id"], canonical(report))
        return report
    app.evaluate = evaluate_and_store
    return app, state


def approval(body, ctx, **changes):
    return StoredException(canonical(body).decode(), ctx.fingerprint, 1, False, **changes)
