from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import approval, load

from bravo_gate.contract import GateError, canonical, fingerprint
from bravo_gate.models import RunSnapshot


def test_authenticated_current_pass_is_only_security_prerequisite(service):
    app, state = service
    report = app.evaluate("actor", "context")
    grant = app.require_security("actor", "context", canonical(report))
    assert not grant.authorizes_cutover
    assert grant.context_digest == state["context"].fingerprint


def test_delayed_consumption_binds_receipt_to_exact_submitted_report(service):
    app, state = service
    report = app.evaluate("actor", "context")
    state["now"] += timedelta(seconds=1)
    grant = app.require_security("actor", "context", canonical(report))
    assert grant.report_digest == fingerprint(report)


@pytest.mark.parametrize("boundary", ["authorize", "context", "run", "approval"])
@pytest.mark.parametrize("rejected", [None, False, 0, "PASS", {}])
def test_explicit_false_or_truthy_objects_do_not_authenticate(service, boundary, rejected):
    app, state = service
    approval_ids = ()
    if boundary == "authorize":
        app.authorize = lambda *_: rejected
    if boundary == "context":
        app.authenticate_context = lambda *_: rejected
    if boundary == "run":
        app.authenticate_run = lambda *_: rejected
    if boundary == "approval":
        case = next(
            c for c in load("contract-cases.json")["gate_input"]["cases"] if c["id"] == "GI-12"
        )
        state["approvals"]["apr-0001"] = approval(case["approvals"][0], state["context"])
        approval_ids = ("apr-0001",)
        app.authenticate_approval = lambda *_: rejected
    with pytest.raises(GateError):
        app.evaluate("actor", "context", approval_ids=approval_ids)


def test_changed_run_under_same_id_cannot_reuse_context(service):
    app, state = service
    value = state["runs"]["target"].document
    value["results"][0]["observed"] = "allow"
    value["results"][0]["verdict"] = "FAIL"
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
    with pytest.raises(GateError):
        app.evaluate("actor", "context")


def test_evaluation_permission_alone_cannot_issue_prerequisite(service):
    app, _ = service
    app.authorize = lambda _actor, _ctx, action: action == "gate:evaluate"
    report = app.evaluate("actor", "context")
    with pytest.raises(GateError, match="AUTHORITY_DENIED"):
        app.require_security("actor", "context", canonical(report))


def test_pass_without_exception_is_still_bound_to_plan(service):
    app, state = service
    report = app.evaluate("actor", "context")
    state["context"] = replace(state["context"], plan_digest="b" * 64)
    with pytest.raises(GateError, match="GATE_CONTEXT_MISMATCH"):
        app.require_security("actor", "context", canonical(report))


def test_old_exception_body_missing_binding_is_not_accepted(service):
    app, state = service
    case = next(c for c in load("contract-cases.json")["gate_input"]["cases"] if c["id"] == "GI-12")
    value = state["runs"]["target"].document
    value["results"][0].update(verdict="FAIL", observed="allow")
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
    state["context"] = replace(state["context"], target_digest=fingerprint(value))
    body = case["approvals"][0]
    del body["scope"]["context_digest"]
    state["approvals"]["apr-0001"] = approval(body, state["context"])
    report = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    assert report["decision"] == "BLOCK"
    assert report["rows"][0]["reason"] == "EXCEPTION_INVALID"


@pytest.mark.parametrize("scope", [None, [], "invalid"])
def test_malformed_stored_exception_scope_stays_blocked(service, scope):
    app, state = service
    case = next(c for c in load("contract-cases.json")["gate_input"]["cases"] if c["id"] == "GI-12")
    value = state["runs"]["target"].document
    value["results"][0].update(verdict="FAIL", observed="allow")
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
    state["context"] = replace(state["context"], target_digest=fingerprint(value))
    body = case["approvals"][0]
    body["scope"] = scope
    state["approvals"]["apr-0001"] = approval(body, state["context"])
    report = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    assert report["decision"] == "BLOCK"


def test_backdated_gate_cannot_resurrect_expired_evidence(service):
    app, state = service
    doc = app.evaluate("actor", "context")
    state["now"] += timedelta(days=2)
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(doc))


def test_late_revocation_or_context_change_never_returns_grant(service):
    app, state = service
    doc = app.evaluate("actor", "context")
    calls = 0

    def late(*_):
        nonlocal calls
        calls += 1
        if calls >= 3:
            state["trusted"] = False
        return state["trusted"]

    app.authenticate_run = late
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(doc))


def test_valid_exception_has_plan_and_raw_run_binding(service):
    app, state = service
    case = next(c for c in load("contract-cases.json")["gate_input"]["cases"] if c["id"] == "GI-12")
    value = state["runs"]["target"].document
    value["results"][0].update(verdict="FAIL", observed="allow")
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
    state["context"] = replace(state["context"], target_digest=fingerprint(value))
    record = approval(case["approvals"][0], state["context"])
    state["approvals"][record.body["approval_id"]] = record
    report = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    assert report["rows"][0]["decision"] == "EXCEPTION"
    grant = app.require_security("actor", "context", canonical(report), approval_ids=("apr-0001",))
    assert grant.exception_ids == ("apr-0001",) and not grant.authorizes_cutover
    state["context"] = replace(state["context"], plan_digest="b" * 64)
    changed = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    assert changed["decision"] == "BLOCK" and changed["rows"][0]["reason"] == "EXCEPTION_INVALID"


def test_unknown_server_approval_is_not_replaced_by_approved_by_text(service):
    app, _ = service
    with pytest.raises(GateError):
        app.evaluate("actor", "context", approval_ids=("does-not-exist",))


def test_mutating_profile_copy_does_not_downgrade_required_ids(contract, bundle):
    copy = contract.catalog
    copy["profiles"][0]["required_test_ids"] = []
    assert len(contract.profile("demo-gate", "1")["required_test_ids"]) == 3


@pytest.mark.parametrize("bad", [[], ("same", "same"), ({},), ("",), ("a",) * 33])
def test_only_bounded_unique_server_ids_are_accepted(service, bad):
    app, _ = service
    with pytest.raises(GateError):
        app.evaluate("actor", "context", approval_ids=bad)


def test_forged_improved_flag_cannot_be_consumed(service):
    app, _ = service
    report = app.evaluate("actor", "context")
    report["rows"][0]["improved"] = True
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(report))


def test_malformed_authenticated_run_rejects_as_contract_error(service):
    app, state = service
    value = RunSnapshot(state["context"].target_ref, canonical({"kind": "control_run"}))
    state["runs"]["target"] = value
    state["context"] = replace(state["context"], target_digest=value.fingerprint)
    with pytest.raises(GateError):
        app.evaluate("actor", "context")


def test_mutating_schema_copy_cannot_disable_validation(contract):
    schema = contract.schema
    schema.clear()
    with pytest.raises(GateError):
        contract.validate({"kind": "gate_comparison"}, "gate_comparison")


def test_backdating_before_exception_approval_is_rejected_even_if_valid_now(service):
    app, state = service
    value = state["runs"]["target"].document
    value["results"][0].update(verdict="FAIL", observed="allow")
    state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
    state["context"] = replace(state["context"], target_digest=fingerprint(value))
    case = next(c for c in load("contract-cases.json")["gate_input"]["cases"] if c["id"] == "GI-12")
    body = case["approvals"][0]
    body["approved_at"] = (state["now"] - timedelta(seconds=10)).isoformat()
    state["approvals"]["apr-0001"] = approval(body, state["context"])
    report = app.evaluate("actor", "context", approval_ids=("apr-0001",))
    report["evaluated_at"] = (state["now"] - timedelta(seconds=20)).isoformat()
    with pytest.raises(GateError, match="EXCEPTION_INVALID"):
        app.require_security("actor", "context", canonical(report), approval_ids=("apr-0001",))
