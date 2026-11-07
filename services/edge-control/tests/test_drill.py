"""Approved Edge drill on a real T10 SQLite store, real OperatorAccess/infra approvals and canonical T36.

The provider is the HTTP mock from test_control; no live plan, approval or Cloudflare resource is used.
"""

import json
from dataclasses import asdict
from datetime import timedelta
from threading import Event

import httpx
import pytest
from bravo_observability.sli import Record, build_window
from bravo_platform_events.infra_approval import InfraApprovalService
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanStore
from bravo_platform_events.store import EventError
from test_control import BINDING, Provider
from test_host import PLAN, PROFILE, SPEC, START

from bravo_edge_control.drill import DrillAuthority, DrillHost, ProofStream, drill_spec, spec_digest
from bravo_edge_control.provider import Cloudflare, EdgeError
from bravo_edge_control.runtime import ShiftRuntime

RUN = "drill-run-1"


class World:
    def __init__(self, path):
        self.now = START
        self.store = PlanStore(path / "plans.sqlite")
        self.store.register_plan(SPEC, run_id="unit-registration", occurred_at=START.isoformat())
        with self.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("UPDATE plans SET state='PROPOSE', version=3 WHERE plan_id=?", (PLAN,))
        at = int(START.timestamp())
        self.approver = OperatorPrincipal("approver", SPEC.tenant_id, at, at + 7200, at, "approver-session")
        self.executor = OperatorPrincipal("executor", SPEC.tenant_id, at, at + 7200, at, "executor-session")
        self.access = OperatorAccess(self.store, clock=self.clock)
        for n, (who, perms) in enumerate([(self.approver, ["read", "approve_infra"]),
                                          (self.executor, ["read", "apply_infra"])]):
            self.access.set_permissions(PLAN, who, permissions=perms, valid_after=START.isoformat(),
                expires_at=(START + timedelta(hours=2)).isoformat(), expected_revision=0,
                administration_ref="fixture-human-decision", command_key=f"grant-{n}")
        self.spec = drill_spec(plan_id=PLAN, run_id=RUN, profile=PROFILE, lb_binding=asdict(BINDING))
        self.upstream = Provider()
        self.provider = Cloudflare(BINDING, lambda: "fixture-token", transport=httpx.MockTransport(self.upstream))

    def clock(self):
        return self.now.isoformat()

    def approve(self, digest=None, requested_by="agent-t17"):
        service = InfraApprovalService(self.store, check_approve=self.access.requires("approve_infra"),
            check_apply=self.access.requires("apply_infra"), check_access=self.access.check_read,
            check_approver=self.access.requires_subject("approve_infra"), clock=self.clock)
        binding = dict(infra_plan_id=RUN, plan_digest=digest or spec_digest(self.spec), requested_by=requested_by)
        return service.approve(self.approver, PLAN, binding=binding, command_key="drill-approval",
                               expires_at=(self.now + timedelta(hours=1)).isoformat())["approval_id"]

    def host(self, approval_id, spec=None):
        authority = DrillAuthority(self.store, spec=spec or self.spec, approval_id=approval_id, clock=self.clock)
        return DrillHost(self.store, self.provider, self.journal, authority=authority, clock=self.clock), authority

    def register(self, approval_id):
        host, authority = self.host(approval_id)
        authority.register(self.executor, PLAN, run_id=RUN, profile=PROFILE, binding=BINDING)
        return host

    def window(self, weight, *, fail=False):
        start = self.now
        self.now += timedelta(seconds=10)
        document = build_window(
            [Record("source", 200, 100)] * (2000 * (100 - weight) // 100)
            + [Record("target", 500 if fail else 200, 100)] * (2000 * weight // 100),
            plan_id=PLAN, start=start.isoformat(), end=self.now.isoformat(),
            pools={"source": "source", "target": "target"})
        with self.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("INSERT INTO sli_windows VALUES (?,?,?,?)",
                      (PLAN, int(start.timestamp() * 1e6), int(self.now.timestamp() * 1e6), json.dumps(document)))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.journal = tmp_path / "edge.sqlite"
    yield w
    w.provider.close()


def test_an_approved_drill_runs_10_50_100_0_once_on_a_plan_before_shift(world):
    host = world.register(world.approve())
    seen = []

    def emit(value):
        if "receipt" in value:
            seen.append(value["receipt"]["weight_target"])
            if value["receipt"]["weight_target"]:
                world.window(value["receipt"]["weight_target"])

    runtime = ShiftRuntime(host, principal=lambda: world.executor, plan_id=PLAN, run_id=RUN)
    result = runtime.sequence(stop=Event(), emit=emit)
    assert result["status"] == "RESTORED" and seen == [10, 50, 100, 0]
    assert len(world.upstream.patches) == 4
    with world.store._transaction() as c:
        assert c.execute("SELECT state, version FROM plans WHERE plan_id=?", (PLAN,)).fetchone()[:] == ("PROPOSE", 3)
    # A restart reads the same journal and sends nothing.
    assert ShiftRuntime(world.host(host.authority.approval_id)[0], principal=lambda: world.executor,
                        plan_id=PLAN, run_id=RUN).sequence(stop=Event())["status"] == "RESTORED"
    assert len(world.upstream.patches) == 4


def test_a_failing_target_window_aborts_to_0_automatically(world):
    host = world.register(world.approve())

    def emit(value):
        if "receipt" in value and value["receipt"]["weight_target"]:
            world.window(value["receipt"]["weight_target"], fail=True)

    result = ShiftRuntime(host, principal=lambda: world.executor, plan_id=PLAN, run_id=RUN).sequence(
        stop=Event(), emit=emit)
    assert result["status"] == "ABORTED" and result["rollback"]["weight_target"] == 0
    assert len(world.upstream.patches) == 2


def test_the_claim_needs_the_exact_spec_the_human_approved(world):
    other = world.spec | {"run_id": "another-run"}
    approval = world.approve(digest=spec_digest(other))
    _, authority = world.host(approval)
    with pytest.raises(EventError, match="binding_mismatch"):
        authority.register(world.executor, PLAN, run_id=RUN, profile=PROFILE, binding=BINDING)
    with pytest.raises(EdgeError, match="drill_approval_unavailable"):
        world.host(approval)[0].start(world.executor, PLAN, "stage-10")
    assert world.upstream.patches == []


def test_no_self_approval_and_no_run_without_apply_infra(world):
    with pytest.raises(EventError, match="self_approval_forbidden"):
        world.approve(requested_by="approver")
    approval = world.approve()
    _, authority = world.host(approval)
    with pytest.raises(EventError, match="forbidden"):
        authority.register(world.approver, PLAN, run_id=RUN, profile=PROFILE, binding=BINDING)
    host = world.register(approval)
    with pytest.raises(EventError, match="forbidden"):
        host.start(world.approver, PLAN, "stage-10")  # the approver alone cannot move weights
    assert world.upstream.patches == []


@pytest.mark.parametrize("change", ["shift", "new_version", "expired", "approver_revoked"])
def test_every_step_rechecks_plan_approval_and_grants(world, change):
    host = world.register(world.approve())
    host.start(world.executor, PLAN, "stage-10")
    if change == "shift":  # the migration itself moved on: a drill never runs beside its own SHIFT
        with world.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("UPDATE plans SET state='SHIFT' WHERE plan_id=?", (PLAN,))
    elif change == "new_version":
        with world.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("UPDATE plans SET version=4 WHERE plan_id=?", (PLAN,))
    elif change == "expired":
        world.now += timedelta(hours=1, seconds=1)
    else:
        world.access.set_permissions(PLAN, world.approver, permissions=[], valid_after=START.isoformat(),
            expires_at=(START + timedelta(hours=2)).isoformat(), expected_revision=1,
            administration_ref="fixture-revocation", command_key="revoke-approver")
    world.window(10)
    with pytest.raises((EdgeError, EventError)):
        host.advance(world.executor, PLAN, "stage-50")
    assert len(world.upstream.patches) == 1


def test_the_operator_must_stay_present_through_the_proof_stream():
    now = [100.0]
    lines = iter(["proof-1", None, None])
    actor = OperatorPrincipal("executor", "tenant-test", 100, 160, 100, "s")
    stream = ProofStream(lambda token: actor if token == "proof-1" else None, lambda: next(lines), now=lambda: now[0])
    assert stream() is actor
    now[0] = 150.0
    assert stream() is actor  # the newest valid proof serves until it expires
    now[0] = 161.0
    with pytest.raises(EdgeError, match="operator_proof_unavailable"):
        stream()
    with pytest.raises(EdgeError, match="operator_proof_unavailable"):
        ProofStream(lambda token: None, lambda: "forged", now=lambda: 100.0)()


def test_the_digest_covers_the_decision_only():
    spec = drill_spec(plan_id="p", run_id="r", profile={"x": 1}, lb_binding={"y": 2})
    assert spec_digest(spec) == spec_digest(json.loads(json.dumps(spec)))
    assert len({spec_digest(spec), spec_digest(spec | {"run_id": "s"}), spec_digest(spec | {"profile": {}})}) == 3
