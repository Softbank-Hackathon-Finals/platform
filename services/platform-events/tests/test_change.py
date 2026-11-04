"""Synthetic T15 authority only; no product rehearsal or deployment is run."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_cutover import BINDING as CUTOVER_BINDING
from test_cutover import BINDINGS, EXPIRES, NOW, PRINCIPAL, SPEC
from test_operator_access import Identity

from bravo_platform_events.change import ChangeApprovalService, CurrentChange
from bravo_platform_events.cutover import CutoverService
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.operator_access import OperatorAccess, create_operator_cutover_app
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.store import EventError

BINDING = {k: v for k, v in CUTOVER_BINDING.items() if k not in ("gate_id", "context_digest")}


class Fixture:
    def __init__(self, backend):
        self.backend, self.now = backend, NOW
        self.context = CurrentChange(dict(BINDING), "rehearsal-1", "verification-1", "revision-1")
        self.allowed = True
        self.calls = []
        backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
        # Only a fixture admin seeds REHEARSE. No runtime transition is added.
        with backend.admin() as c:
            c.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id='plan-demo'")

    def authority(self, c, principal, plan, **kwargs):
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone()["state"] == "REHEARSE"
        self.calls.append(kwargs)
        if not self.allowed:
            raise RuntimeError("PRIVATE_UNVERIFIED_RECEIPT")
        return self.context

    def service(self):
        return ChangeApprovalService(self.backend.reopen(), authority=self.authority,
                                     clock=lambda: self.now)

    def approve(self, **changes):
        return self.service().approve(PRINCIPAL, SPEC.plan_id,
            **(dict(run_id="rehearsal-1", binding=BINDING, expected_version=1, command_key="approval-1") | changes))

    def claim(self, approval, **changes):
        return self.service().claim_application(PRINCIPAL, SPEC.plan_id,
            **(dict(approval_id=approval["approval_id"], command_key="apply-1") | changes))


@pytest.fixture
def change(backend):
    return Fixture(backend)


def test_approval_and_single_claim_survive_reopen_without_advancing_state(change):
    f = change
    approved = f.approve()
    assert f.approve() == approved
    claim, created = f.claim(approved)
    assert created and claim["binding"] == BINDING
    assert f.claim(approved) == (claim, False)
    assert f.backend.reopen().snapshot(SPEC.plan_id)["state"] == "REHEARSE"
    events = f.backend.store.read_after(SPEC.plan_id, None)
    assert [e["event_type"] for e in events] == ["state.changed", "approval.decided"]
    assert events[1]["data"]["approval_kind"] == "change"
    assert events[1]["run_id"] == "rehearsal-1"


@pytest.mark.parametrize("field", list(BINDING))
def test_client_binding_must_match_current_server_candidate(change, field):
    with pytest.raises(EventError, match="binding_mismatch"):
        change.approve(binding=BINDING | {field: "different"})
    assert len(change.backend.store.read_after(SPEC.plan_id, None)) == 1


def test_two_rehearsals_in_one_plan_are_selected_and_consumed_independently(change):
    f = change
    candidates = {"rehearsal-1": f.context, "rehearsal-2": replace(f.context,
        rehearsal_id="rehearsal-2", verification_id="verification-2")}
    # Even identical wire bindings cannot select another task implicitly.
    with pytest.raises(EventError, match="binding_mismatch"):
        f.approve(run_id="rehearsal-2")
    f.authority = lambda _c, _actor, _plan, *, rehearsal_id, **_kwargs: candidates[rehearsal_id]
    first = f.approve()
    with pytest.raises(EventError, match="command_key_conflict"):
        f.approve(run_id="rehearsal-2")
    second = f.approve(run_id="rehearsal-2", command_key="approve-second")
    assert f.claim(second)[0]["verification_id"] == "verification-2"
    assert f.claim(first, command_key="apply-first")[0]["verification_id"] == "verification-1"


@pytest.mark.parametrize("fault", ["expired", "revoked", "version", "state", "diff_hash",
    "image_digest", "target_environment_id", "rehearsal_id", "verification_id", "revision"])
def test_claim_rechecks_current_binding_and_authority(change, fault):
    f = change
    approval = f.approve()
    if fault == "expired":
        f.now = EXPIRES
    elif fault == "revoked":
        f.allowed = False
    elif fault in ("version", "state"):
        with f.backend.admin() as c:
            c.execute("UPDATE plans SET version=2" if fault == "version" else "UPDATE plans SET state='CUTOVER'")
    elif fault in ("rehearsal_id", "verification_id", "revision"):
        f.context = replace(f.context, **{fault: "different"})
    else:
        value = "env-other" if fault == "target_environment_id" else "sha256:" + "f" * 64
        f.context = replace(f.context, binding=BINDING | {fault: value})
    with pytest.raises(EventError):
        f.claim(approval)
    with f.backend.admin() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_commands WHERE command_key LIKE 'change-consumed-%'").fetchone()[0] == 0


def test_no_authority_untrusted_receipt_and_foreign_tenant_fail_closed(change):
    f = change
    kwargs = dict(run_id="rehearsal-1", binding=BINDING, expected_version=1, command_key="key")
    with pytest.raises(EventError, match="authority_unavailable"):
        ChangeApprovalService(f.backend.store).approve(PRINCIPAL, SPEC.plan_id, **kwargs)
    f.context = {"state": "RECEIPT_UNVERIFIED", "untrusted_receipt": {"verdict": "PASS"}}
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()
    f.calls.clear()
    with pytest.raises(EventError, match="forbidden"):
        f.service().approve(Principal("other", "foreign"), SPEC.plan_id, **kwargs)
    assert f.calls == []


def test_concurrent_claims_allow_only_one_new_application(change):
    f = change
    approval = f.approve()
    barrier = Barrier(6)
    def claim(_):
        barrier.wait(timeout=10)
        return f.claim(approval)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert sum(created for _, created in results) == 1
    assert len({r["application_id"] for r, _ in results}) == 1
    with pytest.raises(EventError, match="approval_unavailable"):
        f.claim(approval, command_key="new-key")
    second = f.approve(command_key="new-approval")
    with pytest.raises(EventError, match="candidate_already_claimed"):
        f.claim(second, command_key="second")


def test_different_inputs_cannot_reuse_keys(change):
    f = change
    approval = f.approve()
    with pytest.raises(EventError, match="command_key_conflict"):
        f.approve(expected_version=2)
    f.claim(approval)
    with pytest.raises(EventError, match="command_key_conflict"):
        f.claim({"approval_id": "different"})
    # Kinds share POST /approvals. They cannot reuse its key with new input.
    cutover = CutoverService(f.backend.store, None, bindings=BINDINGS)
    with pytest.raises(EventError, match="command_key_conflict"):
        cutover.approve(PRINCIPAL, SPEC.plan_id, binding=BINDING, expected_version=1,
                        command_key="approval-1")


@pytest.mark.parametrize("table", ["events", "plan_commands"])
def test_approval_is_atomic_with_event(change, table):
    f = change
    with f.backend.reject_insert(table), pytest.raises(f.backend.storage_error):
        f.approve()
    assert len(f.backend.store.read_after(SPEC.plan_id, None)) == 1
    assert f.approve()["approval_kind"] == "change"


def test_claim_storage_failure_does_not_consume_approval(change):
    f = change
    approved = f.approve()
    with f.backend.reject_insert("plan_commands"), pytest.raises(f.backend.storage_error):
        f.claim(approved)
    assert f.claim(approved)[1] is True


def test_commit_acknowledgement_loss_never_grants_a_second_dispatch(change):
    f = change
    approval = f.approve()
    service = f.service()
    original = service.store._transaction
    @contextmanager
    def lose_ack(**kwargs):
        with original(**kwargs) as c:
            yield c
        raise RuntimeError("simulated_commit_ack_loss")
    service.store._transaction = lose_ack
    with pytest.raises(RuntimeError, match="simulated_commit_ack_loss"):
        service.claim_application(PRINCIPAL, SPEC.plan_id,
            approval_id=approval["approval_id"], command_key="apply-1")
    saved, newly_created = f.claim(approval)
    assert saved["approval_id"] == approval["approval_id"] and not newly_created


def test_expiry_during_authority_lookup_is_rejected(change):
    f = change
    original = f.authority
    def delayed(*args, **kwargs):
        current = original(*args, **kwargs)
        f.now = EXPIRES
        return current
    f.authority = delayed
    with pytest.raises(EventError, match="approval_expired"):
        f.approve()


def test_api_routes_change_to_explicit_service_and_hides_private_context(change):
    f = change
    store = f.backend.store
    cutover = CutoverService(store, None, bindings=BINDINGS)
    service = ChangeApprovalService(store, authority=f.authority, clock=lambda: f.now)
    app = create_cutover_app(cutover, change_service=service, resolve_principal=lambda _: PRINCIPAL)
    url = f"/v1/plans/{SPEC.plan_id}/approvals"
    with TestClient(app) as client:
        result = client.post(url, json=dict(approval_kind="change", run_id="rehearsal-1", binding=BINDING, expected_version=1),
                             headers={"Idempotency-Key": "http"})
        assert result.status_code == 200
        assert result.json()["approval_kind"] == "change"
        assert "context" not in result.json() and "revision-1" not in result.text
        assert result.headers["cache-control"] == "no-store"
        result = client.post(f"/v1/plans/{SPEC.plan_id}/cutover",
            json={"approval_id": result.json()["approval_id"]}, headers={"Idempotency-Key": "execute"})
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "approval_unavailable"
        assert client.post(f"/v1/plans/{SPEC.plan_id}/apply").status_code == 404


def test_secured_s2_uses_distinct_permissions_and_rechecks_approver(change):
    f = change
    identity = Identity(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    f.now = "2026-11-03T10:00:01+00:00"
    approver, applier = identity.principal(), identity.principal("human-2")
    access = OperatorAccess(f.backend.store, clock=lambda: f.now)
    def grant(who, permissions, revision=0):
        access.set_permissions(SPEC.plan_id, who, permissions=permissions, valid_after=NOW,
            expires_at=EXPIRES, expected_revision=revision, administration_ref="admin-decision",
            command_key=f"grant-{who.subject_id}-{revision}")
    service = ChangeApprovalService(f.backend.store, authority=access.change_authority(f.authority),
        check_access=access.check_read, clock=lambda: f.now)
    args = dict(run_id="rehearsal-1", binding=BINDING, expected_version=1, command_key="secure")
    grant(approver, ["read", "approve_cutover", "execute_cutover"])
    with pytest.raises(EventError, match="forbidden"):
        service.approve(approver, SPEC.plan_id, **args)
    grant(approver, ["read", "approve_change"], 1)
    grant(applier, ["read", "apply_change"])
    receipt = service.approve(approver, SPEC.plan_id, **args)
    claim = dict(approval_id=receipt["approval_id"], command_key="apply")
    with pytest.raises(EventError, match="forbidden"):
        service.claim_application(approver, SPEC.plan_id, **claim)
    grant(approver, ["read"], 2)
    with pytest.raises(EventError, match="forbidden"):
        service.claim_application(applier, SPEC.plan_id, **claim)
    grant(approver, ["read", "approve_change"], 3)
    with pytest.raises(EventError, match="approval_stale"):
        service.claim_application(applier, SPEC.plan_id, **claim)
    receipt = service.approve(approver, SPEC.plan_id, **(args | {"command_key": "fresh"}))
    access.revoke_approval(SPEC.plan_id, receipt["approval_id"], approval_kind="change",
                           administration_ref="revoked")
    with pytest.raises(EventError, match="forbidden"):
        service.claim_application(applier, SPEC.plan_id,
            **(claim | {"approval_id": receipt["approval_id"]}))
    fresh = service.approve(approver, SPEC.plan_id, **(args | {"command_key": "latest"}))
    assert service.claim_application(applier, SPEC.plan_id,
        **(claim | {"approval_id": fresh["approval_id"]}))[1] is True
    with pytest.raises(EventError, match="approval_already_consumed"):
        access.revoke_approval(SPEC.plan_id, fresh["approval_id"], approval_kind="change",
                               administration_ref="too-late")
    app = create_operator_cutover_app(f.backend.store, None, bindings=BINDINGS,
        verifier=identity.verifier, change_authority=f.authority, clock=lambda: f.now)
    with TestClient(app) as client:
        result = client.post(f"/v1/plans/{SPEC.plan_id}/approvals",
            json=dict(approval_kind="change", **{k: args[k] for k in ("run_id", "binding", "expected_version")}),
            headers={"Authorization": f"Bearer {identity.token()}", "Idempotency-Key": "http-secure"})
        assert result.status_code == 200
        assert "PRIVATE" not in json.dumps(result.json())
