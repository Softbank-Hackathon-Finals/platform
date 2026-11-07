"""Pre-delegated approvals: recorded by the approver once, used by the host per gate, checked like a person."""

from datetime import datetime, timedelta

import pytest
from test_cutover import NOW, SPEC

from bravo_platform_events.approval_delegation import ApprovalDelegations
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanStore
from bravo_platform_events.store import EventError

T0 = datetime.fromisoformat(NOW)


class Clock:
    def __init__(self):
        self.now = T0 + timedelta(minutes=1)

    def __call__(self):
        return self.now.isoformat()


def person(subject, at):
    ts = int(at.timestamp())
    return OperatorPrincipal(subject, SPEC.tenant_id, ts, ts + 300, ts, "login")


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    store = PlanStore(tmp_path / "t10.db")
    store.register_plan(SPEC, run_id="register", occurred_at=NOW)
    access = OperatorAccess(store, clock=clock)
    grant = dict(valid_after=NOW, expires_at=(T0 + timedelta(hours=12)).isoformat(), expected_revision=0,
                 administration_ref="fixture")
    access.set_permissions(SPEC.plan_id, person("approver", T0), permissions=["read", "approve_shift",
                           "approve_cutover"], command_key="grant-approver", **grant)
    access.set_permissions(SPEC.plan_id, person("viewer", T0), permissions=["read"], command_key="grant-viewer", **grant)
    return dict(store=store, access=access, clock=clock, delegations=ApprovalDelegations(store, access, clock=clock),
                approver=person("approver", clock.now))


def delegate(w, **kw):
    args = dict(gates=["shift", "cutover"], bounds={"cutover": {"target_environment_id": "env-onprem"}},
                expires_at=(w["clock"].now + timedelta(hours=4)).isoformat(), command_key="delegate-1") | kw
    return w["delegations"].delegate(w["approver"], SPEC.plan_id, **args)


def test_only_a_holder_of_each_gate_permission_can_delegate_it(world):
    with pytest.raises(EventError, match="forbidden"):
        world["delegations"].delegate(person("viewer", world["clock"].now), SPEC.plan_id, gates=["shift"], bounds={},
                                      expires_at=(world["clock"].now + timedelta(hours=1)).isoformat(),
                                      command_key="d")
    with pytest.raises(EventError, match="forbidden"):  # approve_change was never granted
        delegate(world, gates=["change"], bounds={})
    with pytest.raises(EventError, match="invalid_expiry"):
        delegate(world, expires_at=(world["clock"].now + timedelta(hours=25)).isoformat())
    receipt = delegate(world)
    assert receipt["approver"] == "approver" and receipt["gates"] == ["cutover", "shift"]
    assert delegate(world) == receipt  # the same call again is the same receipt
    with pytest.raises(EventError, match="delegation_exists"):  # never widened in place
        delegate(world, gates=["shift"], bounds={}, command_key="delegate-2")


def test_the_host_principal_is_the_approver_and_passes_the_same_permission_check(world):
    delegate(world)
    principal = world["delegations"].principal_for(SPEC.plan_id, "cutover", version=7,
                                                   binding={"target_environment_id": "env-onprem", "epoch": 7})
    assert principal.subject_id == "approver" and principal.session_id.startswith("delegation:")
    with world["store"]._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        world["access"].require(c, principal, plan, "approve_cutover")  # current grant, as a person
    # The grant is revoked: the same delegation no longer approves anything.
    world["access"].set_permissions(SPEC.plan_id, person("approver", T0), permissions=[], valid_after=NOW,
                                    expires_at=(T0 + timedelta(hours=12)).isoformat(), expected_revision=1,
                                    administration_ref="fixture", command_key="revoke-approver")
    with world["store"]._transaction() as c, pytest.raises(EventError, match="forbidden"):
        world["access"].require(c, principal, plan, "approve_cutover")


def test_gate_scope_use_expiry_and_revocation(world):
    delegate(world)
    d = world["delegations"]
    with pytest.raises(EventError, match="approval_not_delegated"):
        d.principal_for(SPEC.plan_id, "failback", version=7, binding={})
    with pytest.raises(EventError, match="delegation_scope_mismatch"):
        d.principal_for(SPEC.plan_id, "cutover", version=7, binding={"target_environment_id": "elsewhere"})
    d.principal_for(SPEC.plan_id, "shift", version=7, binding={"a": "1"})
    d.principal_for(SPEC.plan_id, "shift", version=7, binding={"a": "1"})  # the same decision again
    with pytest.raises(EventError, match="delegation_already_used"):  # another decision for that version
        d.principal_for(SPEC.plan_id, "shift", version=7, binding={"a": "2"})
    with pytest.raises(EventError, match="forbidden"):  # a viewer cannot revoke someone's delegation
        d.revoke(person("viewer", world["clock"].now), SPEC.plan_id)
    d.revoke(world["approver"], SPEC.plan_id)
    with pytest.raises(EventError, match="delegation_revoked"):
        d.principal_for(SPEC.plan_id, "shift", version=8, binding={})
    receipt = delegate(world, command_key="delegate-3", gates=["shift"], bounds={},
                       expires_at=(world["clock"].now + timedelta(minutes=30)).isoformat())
    world["clock"].now += timedelta(minutes=31)
    with pytest.raises(EventError, match="delegation_expired"):
        d.principal_for(SPEC.plan_id, "shift", version=9, binding={})
    assert receipt["delegation_id"]


def test_the_operator_api_records_and_revokes_a_delegation(world):
    from fastapi.testclient import TestClient
    from test_cutover import BINDINGS

    from bravo_platform_events.cutover import CutoverService
    from bravo_platform_events.cutover_api import create_cutover_app

    people = {"approver-token": world["approver"], "viewer-token": person("viewer", world["clock"].now)}
    app = create_cutover_app(CutoverService(world["store"], None, bindings=BINDINGS, clock=world["clock"]),
                             resolve_principal=lambda r: people.get(r.headers.get("authorization", "")[7:]),
                             delegation_service=world["delegations"])
    http = TestClient(app)
    body = {"gates": ["shift"], "expires_at": (world["clock"].now + timedelta(hours=1)).isoformat()}
    denied = http.post(f"/v1/plans/{SPEC.plan_id}/delegations", json=body,
                       headers={"Authorization": "Bearer viewer-token", "Idempotency-Key": "d1"})
    assert denied.status_code == 403
    made = http.post(f"/v1/plans/{SPEC.plan_id}/delegations", json=body,
                     headers={"Authorization": "Bearer approver-token", "Idempotency-Key": "d1"})
    assert made.status_code == 201 and made.json()["gates"] == ["shift"]
    assert http.post(f"/v1/plans/{SPEC.plan_id}/delegations", json=body | {"extra": 1},
                     headers={"Authorization": "Bearer approver-token", "Idempotency-Key": "d2"}).status_code == 422
    revoked = http.post(f"/v1/plans/{SPEC.plan_id}/delegations:revoke", headers={"Authorization": "Bearer approver-token"})
    assert revoked.json() == {"revoked": made.json()["delegation_id"]}


def test_a_delegated_shift_runs_the_edge_once_per_version_until_100(world, monkeypatch):
    from bravo_platform_events.approval_delegation import DelegatedGates, DelegatedShift

    with world["store"]._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("UPDATE plans SET state='SHIFT' WHERE plan_id=?", (SPEC.plan_id,))
    started = []
    shift = DelegatedShift(world["store"], world["delegations"], provider=None, journal=None, profile={},
                           load_session=None)
    monkeypatch.setattr(shift, "run", lambda plan_id, version: started.append((plan_id, version)))
    gates = DelegatedGates(world["store"], world["delegations"], cutover=None, shift=shift)
    assert gates.sweep() == {}  # nothing delegated yet: an operator runs SHIFT
    delegate(world, gates=["cutover"])
    assert gates.sweep() == {}  # cutover alone never moves the Edge
    world["delegations"].revoke(world["approver"], SPEC.plan_id)
    delegate(world, command_key="delegate-shift")
    assert gates.sweep()[SPEC.plan_id] == {"shift": "started"}
    shift.runs[next(iter(shift.runs))].join()
    assert gates.sweep()[SPEC.plan_id] == {"shift": "finished"}  # one attempt per version
    assert started == [(SPEC.plan_id, world["store"].snapshot(SPEC.plan_id)["version"])]
