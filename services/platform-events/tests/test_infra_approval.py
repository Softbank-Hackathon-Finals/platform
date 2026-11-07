"""T44 destination build approval on a migration plan (infra_approval, ADR-0032)."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_cutover import EXPIRES, NOW, PRINCIPAL, SPEC
from test_workflow_recovery import collect, service

from bravo_platform_events import infra_approval
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.infra_approval import InfraApprovalService
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.store import EventError

PLAN = SPEC.plan_id
LATER = (datetime.fromisoformat(NOW) + timedelta(hours=1)).isoformat()
BINDING = {"infra_plan_id": "infra-1", "plan_digest": "c" * 64, "requested_by": "agent-run-1"}


def allow(*_):
    return None


def infra(store, clock=lambda: NOW, **checks):
    return InfraApprovalService(store, **({"check_approve": allow, "check_apply": allow, "check_access": allow,
                                           "check_approver": allow} | checks), clock=clock)


def test_a_human_approves_one_digest_and_one_run_claims_it(backend):
    collect(backend.store)
    service_ = infra(backend.store)
    receipt = service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER, command_key="a1")
    assert (receipt["approval_kind"], receipt["binding"], receipt["actor_id"]) == ("infra", BINDING, "operator")
    assert service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER, command_key="a1") == receipt
    with pytest.raises(EventError, match="command_key_conflict"):
        service_.approve(PRINCIPAL, PLAN, binding=BINDING | {"plan_digest": "d" * 64}, expires_at=LATER,
                         command_key="a1")

    approval_id = receipt["approval_id"]
    with pytest.raises(EventError, match="binding_mismatch"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING | {"plan_digest": "d" * 64})
    claimed = service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING)
    assert (claimed["run_id"], claimed["approved_by"]) == ("run-1", "operator")
    assert service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING) == claimed
    with pytest.raises(EventError, match="approval_already_consumed"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-2", binding=BINDING)
    assert service_.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] == "run-1"


def test_a_claim_must_name_the_approved_plan_not_only_its_digest(backend):
    """Two infra plans can render the same digest (same stack, request and target): an approval of
    one must not start a run of the other, or a run attributed to another requester."""
    collect(backend.store)
    service_ = infra(backend.store)
    approval_id = service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER, command_key="a1")["approval_id"]
    for other in ({"infra_plan_id": "infra-2"}, {"requested_by": "agent-run-2"}):
        with pytest.raises(EventError, match="binding_mismatch"):
            service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING | other)
    with pytest.raises(ValueError, match="invalid_infra_binding"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding={"plan_digest": "c" * 64})
    assert service_.read(PRINCIPAL, PLAN, approval_id)["claimed_run_id"] is None
    assert service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING)["run_id"] == "run-1"


def test_a_claim_recorded_by_digest_only_still_replays_for_the_same_run(backend):
    """A run claimed before this change (request keyed by digest) resumes after it."""
    from bravo_platform_events.cutover import _put
    from bravo_platform_events.infra_approval import infra_key

    collect(backend.store)
    service_ = infra(backend.store)
    approval_id = service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER, command_key="a1")["approval_id"]
    claimed = dict(approval_id=approval_id, run_id="run-1", binding=BINDING, approved_by="operator",
                   claimed_by="operator", claimed_at=NOW)
    with backend.store._transaction(write=True, plan_id=PLAN) as c:  # what the old claim wrote
        _put(c, PLAN, infra_key("consumed", approval_id),
             {"approval_id": approval_id, "run_id": "run-1", "plan_digest": "c" * 64}, claimed)
    assert service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING) == claimed
    with pytest.raises(EventError, match="approval_already_consumed"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING | {"infra_plan_id": "infra-2"})


def test_the_requester_cannot_approve_and_the_binding_is_exact(backend):
    collect(backend.store)
    service_ = infra(backend.store)
    with pytest.raises(EventError, match="self_approval_forbidden"):
        service_.approve(PRINCIPAL, PLAN, binding=BINDING | {"requested_by": "operator"}, expires_at=LATER,
                         command_key="self")
    for bad in ({**BINDING, "extra": "x"}, {**BINDING, "plan_digest": "C" * 64}, {"infra_plan_id": "infra-1"}):
        with pytest.raises(ValueError, match="invalid_infra_binding"):
            service_.approve(PRINCIPAL, PLAN, binding=bad, expires_at=LATER, command_key="bad")
    too_long = (datetime.fromisoformat(NOW) + timedelta(hours=25)).isoformat()
    for expiry in (NOW, too_long):
        with pytest.raises(EventError, match="invalid_expiry"):
            service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=expiry, command_key="exp")
    with pytest.raises(EventError, match="forbidden"):
        service_.approve(Principal("operator", "tenant-other"), PLAN, binding=BINDING, expires_at=LATER,
                         command_key="tenant")


def test_only_before_shift_and_not_after_expiry(backend, monkeypatch):
    collect(backend.store)
    now = [NOW]
    service_ = infra(backend.store, clock=lambda: now[0])
    approval_id = service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER,
                                   command_key="a1")["approval_id"]
    now[0] = LATER
    with pytest.raises(EventError, match="approval_expired"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING)
    now[0] = NOW
    monkeypatch.setattr(infra_approval, "INFRA_APPROVAL_STATES", frozenset({"SHIFT"}))
    with pytest.raises(EventError, match="plan_state_conflict"):
        service_.claim(PRINCIPAL, PLAN, approval_id, run_id="run-1", binding=BINDING)
    with pytest.raises(EventError, match="plan_state_conflict"):
        service_.approve(PRINCIPAL, PLAN, binding=BINDING, expires_at=LATER, command_key="a2")


def _grant(access, subject, permissions, key):
    now = int(datetime.fromisoformat(NOW).timestamp())
    access.set_permissions(PLAN, OperatorPrincipal(subject, PRINCIPAL.tenant_id, now, now + 300, now, "s"),
                           permissions=permissions, valid_after=NOW, expires_at=EXPIRES,
                           expected_revision=0, administration_ref="fixture-admin", command_key=key)


def _operator(subject):
    now = int(datetime.fromisoformat(NOW).timestamp())
    return OperatorPrincipal(subject, PRINCIPAL.tenant_id, now, now + 300, now, "s")


def test_operator_grants_revocation_and_the_approver_still_holding_approve_infra(backend):
    collect(backend.store)
    access = OperatorAccess(backend.store, clock=lambda: NOW)
    service_ = InfraApprovalService(backend.store, check_approve=access.requires("approve_infra"),
                                    check_apply=access.requires("apply_infra"), check_access=access.check_read,
                                    check_approver=access.requires_subject("approve_infra"), clock=lambda: NOW)
    approver, applier = _operator("approver"), _operator("applier")
    with pytest.raises(EventError, match="forbidden"):
        service_.approve(approver, PLAN, binding=BINDING, expires_at=EXPIRES, command_key="a1")
    _grant(access, "approver", ["read", "approve_infra"], "grant-approver")
    _grant(access, "applier", ["read", "apply_infra"], "grant-applier")
    first = service_.approve(approver, PLAN, binding=BINDING, expires_at=EXPIRES, command_key="a1")["approval_id"]
    with pytest.raises(EventError, match="forbidden"):  # the approver cannot claim without apply_infra
        service_.claim(approver, PLAN, first, run_id="run-1", binding=BINDING)

    access.revoke_approval(PLAN, first, administration_ref="fixture-admin", approval_kind="infra")
    with pytest.raises(EventError, match="forbidden"):
        service_.claim(applier, PLAN, first, run_id="run-1", binding=BINDING)
    assert service_.read(applier, PLAN, first)["revoked"] is True

    second = service_.approve(approver, PLAN, binding=BINDING, expires_at=EXPIRES, command_key="a2")["approval_id"]
    access.set_permissions(PLAN, approver, permissions=["read"], valid_after=NOW, expires_at=EXPIRES,
                           expected_revision=1, administration_ref="fixture-admin", command_key="revoke-approver")
    with pytest.raises(EventError, match="forbidden"):  # approver lost approve_infra after approving
        service_.claim(applier, PLAN, second, run_id="run-2", binding=BINDING)


def test_operator_routes_approve_read_and_claim(backend):
    collect(backend.store)
    app = create_cutover_app(service(backend.store, None), resolve_principal=lambda request: PRINCIPAL,
                             infra_service=infra(backend.store))
    client = TestClient(app)
    body = {"approval_kind": "infra", "binding": BINDING, "expires_at": LATER}
    response = client.post(f"/v1/plans/{PLAN}/approvals", json=body, headers={"Idempotency-Key": "i1"})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"approval_id", "approval_kind", "binding", "actor_id", "approved_at",
                                    "expires_at"}
    approval_id = response.json()["approval_id"]
    assert client.get(f"/v1/plans/{PLAN}/infra-approvals/{approval_id}").json()["revoked"] is False
    claim = client.post(f"/v1/plans/{PLAN}/infra-approvals/{approval_id}/claims",
                        json={"run_id": "run-1", "binding": BINDING | {"plan_digest": "d" * 64}})
    assert (claim.status_code, claim.json()) == (409, {"detail": {"code": "binding_mismatch"}})
    claim = client.post(f"/v1/plans/{PLAN}/infra-approvals/{approval_id}/claims",
                        json={"run_id": "run-1", "binding": BINDING})
    assert claim.status_code == 200 and claim.json()["run_id"] == "run-1"
    bad = client.post(f"/v1/plans/{PLAN}/approvals", json=body | {"binding": BINDING | {"x": 1}},
                      headers={"Idempotency-Key": "i2"})
    assert bad.status_code == 422


def test_without_an_infra_service_the_kind_is_unsupported(backend):
    collect(backend.store)
    client = TestClient(create_cutover_app(service(backend.store, None), resolve_principal=lambda r: PRINCIPAL))
    body = {"approval_kind": "infra", "binding": BINDING, "expires_at": LATER}
    response = client.post(f"/v1/plans/{PLAN}/approvals", json=body, headers={"Idempotency-Key": "i1"})
    assert (response.status_code, response.json()) == (422, {"detail": {"code": "unsupported_approval_kind"}})
    assert client.get(f"/v1/plans/{PLAN}/infra-approvals/x").status_code == 503
