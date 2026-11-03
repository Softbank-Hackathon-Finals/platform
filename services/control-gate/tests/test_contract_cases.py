"""Execute all 35 T5 PR51 review regressions, not merely schema acceptance."""

from copy import deepcopy
from dataclasses import replace

import jsonpatch
import pytest
from conftest import approval, load

from bravo_gate.contract import GateError, canonical, fingerprint
from bravo_gate.engine import consume, evaluate
from bravo_gate.models import RunSnapshot
from bravo_gate.service import registered_result

CASES = load("contract-cases.json")


@pytest.mark.parametrize("case", CASES["evaluation_binding"]["cases"], ids=lambda c: c["id"])
def test_fixed_evaluation_approval_cannot_be_rebound(service, case):
    app, state = service
    base = next(c for c in CASES["gate_input"]["cases"] if c["id"] == "GI-12")
    target = jsonpatch.apply_patch(state["runs"]["target"].document, base["patch"]["target"])
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(target))
    state["context"] = replace(state["context"], target_digest=fingerprint(target))
    record = approval(base["approvals"][0], state["context"])
    state["approvals"]["apr-0001"] = record
    report = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    assert report["decision"] == "PASS" and report["rows"][0]["decision"] == "EXCEPTION"
    assert app.require_security(
        "actor", "context", canonical(report), approval_ids=("apr-0001",)
    ).exception_ids == ("apr-0001",)

    from dataclasses import asdict

    context = jsonpatch.apply_patch(asdict(state["context"]), case.get("context_patch", []))
    for side in ("source", "target"):
        run = jsonpatch.apply_patch(state["runs"][side].document, case.get(side + "_patch", []))
        state["runs"][side] = RunSnapshot(context[side + "_ref"], canonical(run))
        context[side + "_digest"] = fingerprint(run)
    state["context"] = type(state["context"])(**context)
    report = jsonpatch.apply_patch(report, case.get("document_patch", []))
    if case.get("rewrite_document_binding"):
        report["gate_id"] = state["context"].gate_id
        report["context_digest"] = state["context"].fingerprint
        for side in ("source", "target"):
            report[side + "_run_id"] = state["runs"][side].document["run_id"]
    if case.get("rewrite_exception_binding"):
        report["rows"][0]["exception"]["scope"]["context_digest"] = state["context"].fingerprint
    # These documents are structurally valid: the server binding must reject.
    app.contract.validate(report, "gate_comparison")
    if case["expect"] == "accept":
        app.require_security("actor", "context", canonical(report), approval_ids=("apr-0001",))
    else:
        with pytest.raises(GateError, match="GATE_CONTEXT_MISMATCH|EXCEPTION_INVALID"):
            app.require_security("actor", "context", canonical(report), approval_ids=("apr-0001",))
    assert state["approvals"]["apr-0001"] == record


def test_context_vectors_match_gate_examples(contract):
    from bravo_gate.models import GateContext

    for name, body in load("evaluation-contexts.json").items():
        context = GateContext(**body)
        assert context.catalog_digest == contract.catalog_digest
        report = load("examples/gate-" + name + ".json")
        contract.validate(report, "gate_comparison")
        assert report["context_digest"] == context.fingerprint
        assert report["gate_id"] == context.gate_id


@pytest.mark.parametrize("case", CASES["gate_input"]["cases"], ids=lambda c: c["id"])
def test_t19_contract_case(contract, bundle, case):
    ctx, source, target, now = bundle
    source = jsonpatch.apply_patch(source, case["patch"].get("source", []), in_place=False)
    target = jsonpatch.apply_patch(target, case["patch"].get("target", []), in_place=False)
    ctx = replace(ctx, source_digest=fingerprint(source), target_digest=fingerprint(target))
    records = tuple(approval(body, ctx) for body in case["approvals"])
    expected = case["expect"]
    if expected["schema"] == "reject":
        with pytest.raises(GateError):
            evaluate(contract, ctx, source, target, now=now, approvals=records)
        return
    report = evaluate(contract, ctx, source, target, now=now, approvals=records)
    assert report["decision"] == expected["decision"]
    for test_id, reason in expected.get("rows", {}).items():
        assert next(r for r in report["rows"] if r["test_id"] == test_id)["reason"] == reason
    assert report["run_issues"] == expected.get("run_issues", [])
    assert len(report["rows"]) == 3


@pytest.mark.parametrize("case", CASES["gate_document"]["cases"], ids=lambda c: c["id"])
def test_t10_t21_consumption_contract_case(contract, bundle, case):
    ctx, source, target, now = bundle
    document = jsonpatch.apply_patch(
        load(case.get("base", "examples/gate-after-fix.json")), case["patch"], in_place=False
    )
    ctx = replace(ctx, source_digest=fingerprint(source), target_digest=fingerprint(target))
    records = tuple(approval(body, ctx) for body in case["approvals"])
    server = case.get("server_result", CASES["gate_document"]["server_result"])
    registered = load(server["document"])
    contexts = load("evaluation-contexts.json")
    if registered["gate_id"] != ctx.gate_id:
        ctx = type(ctx)(**contexts["before-fix"])
        target = load("examples/target-run-before-fix.json")
    def provider(*_):
        if server["status"] == "unavailable":
            raise RuntimeError("server offline")
        if server["status"] == "missing":
            return None
        return RunSnapshot(ctx.gate_id, canonical(registered))
    def check():
        registered_result("actor", ctx, document, provider=provider,
                          authenticate=lambda *_: server["status"] == "available",
                          deadlines=[], clock=lambda: now)
        return consume(contract, ctx, document, source, target, now=now, approvals=records)
    if case["expect"].get("consumer") == "accept":
        assert check()["decision"] == "PASS"
    else:
        with pytest.raises(GateError):
            check()


def test_full_profile_keeps_all_20_tests_and_missing_dr_blocks(contract, bundle):
    from dataclasses import replace

    ctx, source, target, now = bundle
    report = evaluate(contract, replace(ctx, profile_id="full"), source, target, now=now)
    assert report["decision"] == "BLOCK" and len(report["rows"]) == 20
    assert (
        next(r for r in report["rows"] if r["test_id"] == "DR-01.1")["reason"] == "MISSING_RESULT"
    )


def test_same_input_and_time_is_deterministic(contract, bundle):
    ctx, source, target, now = bundle
    first = evaluate(contract, ctx, source, target, now=now)
    assert first == evaluate(contract, ctx, deepcopy(source), deepcopy(target), now=now)


def test_expired_evidence_and_method_cannot_be_waived(contract, bundle):
    ctx, source, target, now = bundle
    case = next(c for c in CASES["gate_input"]["cases"] if c["id"] == "GI-12")
    target = jsonpatch.apply_patch(target, case["patch"]["target"])
    ctx = replace(ctx, source_digest=fingerprint(source), target_digest=fingerprint(target))
    records = tuple(approval(body, ctx) for body in case["approvals"])
    for change, want in [("expiry", "EVIDENCE_EXPIRED"), ("method", "METHOD_MISMATCH")]:
        changed = deepcopy(target)
        if change == "expiry":
            changed["results"][0]["evidence"]["expires_at"] = "2026-11-04T11:35:00Z"
        else:
            changed["results"][0]["method_version"] = "2"
        report = evaluate(contract, ctx, source, changed, now=now, approvals=records)
        assert report["rows"][0]["reason"] == want and report["decision"] == "BLOCK"
