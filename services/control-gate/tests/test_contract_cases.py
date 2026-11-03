"""Execute all 28 T5 PR51 review regressions, not merely schema acceptance."""

from copy import deepcopy

import jsonpatch
import pytest
from conftest import approval, load

from bravo_gate.contract import GateError
from bravo_gate.engine import consume, evaluate

CASES = load("contract-cases.json")


@pytest.mark.parametrize("case", CASES["gate_input"]["cases"], ids=lambda c: c["id"])
def test_t19_contract_case(contract, bundle, case):
    ctx, source, target, now = bundle
    source = jsonpatch.apply_patch(source, case["patch"].get("source", []), in_place=False)
    target = jsonpatch.apply_patch(target, case["patch"].get("target", []), in_place=False)
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
        load("examples/gate-after-fix.json"), case["patch"], in_place=False
    )
    records = tuple(approval(body, ctx) for body in case["approvals"])
    if case["expect"].get("consumer") == "accept":
        assert (
            consume(contract, ctx, document, source, target, now=now, approvals=records)["decision"]
            == "PASS"
        )
    else:
        with pytest.raises(GateError):
            consume(contract, ctx, document, source, target, now=now, approvals=records)


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
    records = tuple(approval(body, ctx) for body in case["approvals"])
    for change, want in [("expiry", "EVIDENCE_EXPIRED"), ("method", "METHOD_MISMATCH")]:
        changed = deepcopy(target)
        if change == "expiry":
            changed["results"][0]["evidence"]["expires_at"] = "2026-11-04T11:35:00Z"
        else:
            changed["results"][0]["method_version"] = "2"
        report = evaluate(contract, ctx, source, changed, now=now, approvals=records)
        assert report["rows"][0]["reason"] == want and report["decision"] == "BLOCK"
