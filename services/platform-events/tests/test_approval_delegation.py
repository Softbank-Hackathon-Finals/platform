"""Pre-delegated approvals: recorded by the approver once, used by the host per gate, checked like a person."""

import time
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


def test_a_person_delegates_evidence_over_http_and_a_narrowed_grant_stops_its_use(world):
    """ADR-0038: the T19 evidence delegation is a person's own HTTP call, checked again at each use."""
    from fastapi.testclient import TestClient
    from test_cutover import BINDINGS

    from bravo_platform_events.cutover import CutoverService
    from bravo_platform_events.cutover_api import create_cutover_app

    grant = dict(valid_after=NOW, expires_at=(T0 + timedelta(hours=12)).isoformat(), administration_ref="fixture")
    world["access"].set_permissions(SPEC.plan_id, person("issuer", T0), permissions=["read", "gate:publish-provenance"],
                                    command_key="grant-issuer", expected_revision=0, **grant)
    people = {"issuer-token": person("issuer", world["clock"].now)}
    http = TestClient(create_cutover_app(CutoverService(world["store"], None, bindings=BINDINGS, clock=world["clock"]),
                                         resolve_principal=lambda r: people.get(r.headers.get("authorization", "")[7:]),
                                         delegation_service=world["delegations"]))
    made = http.post(f"/v1/plans/{SPEC.plan_id}/delegations",
                     json={"gates": ["evidence"], "expires_at": (world["clock"].now + timedelta(hours=1)).isoformat()},
                     headers={"Authorization": "Bearer issuer-token", "Idempotency-Key": "e1"})
    assert made.status_code == 201 and made.json()["gates"] == ["evidence"]
    use = dict(binding={"context_digest": "a" * 64}, version=None)
    assert world["delegations"].principal_for(SPEC.plan_id, "evidence", **use).subject_id == "issuer"
    world["access"].set_permissions(SPEC.plan_id, person("issuer", T0), permissions=["read"],
                                    command_key="narrow-issuer", expected_revision=1, **grant)
    with pytest.raises(EventError, match="forbidden"):
        world["delegations"].principal_for(SPEC.plan_id, "evidence", **use)


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


def test_auto_records_every_gate_as_the_standing_approver_and_renews_after_expiry(world):
    w = world
    w["access"].set_permissions(SPEC.plan_id, person("platform-approver", T0), permissions=[
        "read", "approve_change", "approve_shift", "approve_cutover", "approve_failback", "start_rehearsal"],
        valid_after=NOW, expires_at=(T0 + timedelta(hours=48)).isoformat(), expected_revision=0,
        administration_ref="fixture", command_key="grant-platform")
    first = w["delegations"].auto(SPEC.plan_id, "platform-approver")
    assert first["approver"] == "platform-approver" and first["gates"] == sorted(
        ["change", "shift", "cutover", "failback", "rehearsal"])
    assert w["delegations"].auto(SPEC.plan_id, "platform-approver") is None  # a live one is kept
    w["clock"].now += timedelta(hours=25)
    renewed = w["delegations"].auto(SPEC.plan_id, "platform-approver")
    assert renewed["delegation_id"] != first["delegation_id"]
    w["delegations"].revoke(person("platform-approver", w["clock"].now), SPEC.plan_id)
    with pytest.raises(EventError, match="forbidden"):  # the standing approver still needs each permission
        w["delegations"].auto(SPEC.plan_id, "viewer")


def test_a_reissued_grant_keeps_an_earlier_authentication(world):
    w = world
    receipt = delegate(w)
    w["access"].set_permissions(SPEC.plan_id, person("approver", w["clock"].now), permissions=[
        "read", "approve_shift", "approve_cutover", "security:read"], valid_after=(w["clock"].now + timedelta(
            minutes=5)).isoformat(), expires_at=(T0 + timedelta(hours=12)).isoformat(), expected_revision=1,
        administration_ref="system-update", command_key="regrant")
    w["clock"].now += timedelta(minutes=10)
    principal = w["delegations"].principal_for(SPEC.plan_id, "shift", binding={}, version=1)
    with w["store"]._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        w["access"].require(c, principal, plan, "approve_shift")  # no longer forbidden after the re-issue
    assert principal.subject_id == receipt["approver"]


def test_a_delegated_shift_makes_its_principal_outside_the_plan_transaction(world, monkeypatch):
    import sys
    import types

    from bravo_platform_events import approval_delegation as ad

    made, inside = [], []
    delegations = types.SimpleNamespace(principal_for=lambda *a, **k: made.append(1) or types.SimpleNamespace(
        expires_at=time.time() + 300))

    class Runtime:
        def __init__(self, host, *, principal, plan_id, run_id):
            self.principal = principal

        def sequence(self, *, stop, hold_at_full):
            inside.append(self.principal())  # read again inside ShiftRuntime's transaction
            return {"status": "AT_FULL"}

    authority = types.SimpleNamespace(register=lambda *a, **k: None)
    edge = {"bravo_edge_control.runtime": types.SimpleNamespace(ShiftRuntime=Runtime),
            "bravo_edge_control.authority": types.SimpleNamespace(ShiftAuthority=lambda *a, **k: authority),
            "bravo_edge_control.host": types.SimpleNamespace(ShiftHost=lambda *a, **k: None),
            "bravo_edge_control.control": types.SimpleNamespace(_now=lambda: "now")}
    for name, module in edge.items():
        monkeypatch.setitem(sys.modules, name, module)
    shift = ad.DelegatedShift(world["store"], delegations, provider=types.SimpleNamespace(binding=None), journal=None,
                              profile={}, load_session=None)
    assert shift.run(SPEC.plan_id, 7) == {"status": "AT_FULL"}
    assert len(made) == 1 and len(inside) == 1  # made once before the run, reused inside it
