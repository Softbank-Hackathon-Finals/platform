"""Stages after PROPOSE, and one plan driven by the workflow host from ANALYZE to DONE."""

import threading
from datetime import datetime, timedelta

import pytest
from test_application_host import host_case  # noqa: F401  (fixture)
from test_change_intake import keys  # noqa: F401  (fixture)
from test_cutover import BINDING, NOW, PRINCIPAL, SPEC
from test_workflow_recovery import Producers, admit_change, collect, remote_executor, service

from bravo_platform_events.executor import ExecutorClient
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import STAGES, WorkflowRecovery
from bravo_platform_events.workflow_host import StageReaders, WorkflowHost
from bravo_platform_events.workflow_stages import (
    ShiftApprovalService,
    application_stages,
    integrity_reader,
    shift_approval_reader,
    watch_reader,
)

PLAN = SPEC.plan_id
LATER = (datetime.fromisoformat(NOW) + timedelta(hours=1)).isoformat()
STEPS_OK = [{"step": "compare", "status": "succeeded"}, {"step": "commit", "status": "succeeded"}]


@pytest.fixture
def steps(monkeypatch):
    """The fixture Executor reports these steps on every read (compare/commit by default)."""
    import test_workflow_recovery as recovery_tests

    current = {"steps": STEPS_OK}
    original = recovery_tests.response_value
    monkeypatch.setattr(recovery_tests, "response_value", lambda: original() | {"steps": current["steps"]})
    return current


def driver(store, producers, client, observed):
    shift = ShiftApprovalService(store, check_approve=lambda c, p, plan: None, clock=lambda: NOW)
    readers = StageReaders({
        "ANALYZE": producers.read, "PROPOSE": producers.read,  # owners' fixtures (T16, #521)
        "REHEARSE": producers.read, "GATE": producers.read,    # T31/T19: tested below and in #519 suites
        "AWAIT_APPROVAL": shift_approval_reader, "CUTOVER": integrity_reader,
        "WATCH": watch_reader(observe=lambda *a: observed["sli"], retest=lambda *a: observed["retest"]),
    })
    cutover = service(store, client)
    return WorkflowHost(WorkflowRecovery(cutover, read_evidence=readers)), shift, cutover


def test_host_drives_one_plan_from_analyze_to_done(backend, tmp_path, steps):
    store = backend.store
    ledger = tmp_path / "remote.db"
    producers = Producers(ledger)
    observed = {"sli": "UNKNOWN", "retest": "PASS"}
    reply = threading.Event()
    reply.set()
    with (remote_executor(ledger, threading.Event(), reply) as origin,
          ExecutorClient(origin, token="fixture-read", command_token="fixture-command") as client):
        host, shift, cutover = driver(store, producers, client, observed)
        collect(store)
        assert host.sweep()[0].after == "REHEARSE"           # waits for the approved T31 application
        producers.application = admit_change(store)
        [step] = host.sweep()
        assert (step.after, step.hold) == ("AWAIT_APPROVAL", "shift_approval_required")
        version = store.snapshot(PLAN)["version"]
        shift.approve(PRINCIPAL, PLAN, expected_version=version, expires_at=LATER, command_key="shift-1")
        assert host.sweep()[0].after == "SHIFT"              # the operator owns SHIFT -> CUTOVER
        assert host.active() == []
        receipt = cutover.approve(PRINCIPAL, PLAN, binding=BINDING,
                                  expected_version=store.snapshot(PLAN)["version"], command_key="cutover-1")
        cutover.submit(PRINCIPAL, PLAN, approval_id=receipt["approval_id"], command_key="submit-1")
        [step] = host.sweep()
        assert (step.before, step.after) == ("CUTOVER", "WATCH")
        assert step.hold == "workflow_condition_unmet"       # T36 still UNKNOWN: no DONE
        observed["sli"] = "PASS"
        [step] = host.sweep()
        assert step.after == "DONE" and store.snapshot(PLAN)["state"] == "DONE"
        assert host.active() == []


def test_cutover_without_compare_and_commit_is_not_integrity(backend, tmp_path, steps):
    store = backend.store
    ledger = tmp_path / "remote.db"
    producers = Producers(ledger)
    steps["steps"] = [{"step": "compare", "status": "unknown"}, {"step": "commit", "status": "succeeded"}]
    reply = threading.Event()
    reply.set()
    with (remote_executor(ledger, threading.Event(), reply) as origin,
          ExecutorClient(origin, token="fixture-read", command_token="fixture-command") as client):
        host, shift, cutover = driver(store, producers, client, {"sli": "PASS", "retest": "PASS"})
        collect(store)
        host.sweep()
        producers.application = admit_change(store)
        host.sweep()
        shift.approve(PRINCIPAL, PLAN, expected_version=store.snapshot(PLAN)["version"], expires_at=LATER,
                      command_key="shift-1")
        host.sweep()
        receipt = cutover.approve(PRINCIPAL, PLAN, binding=BINDING,
                                  expected_version=store.snapshot(PLAN)["version"], command_key="cutover-1")
        cutover.submit(PRINCIPAL, PLAN, approval_id=receipt["approval_id"], command_key="submit-1")
        [step] = host.sweep()
        assert (step.after, step.hold) == ("CUTOVER", "workflow_condition_unmet")


def test_shift_approval_is_bounded_and_only_at_await_approval(backend, tmp_path):
    store = backend.store
    producers = Producers(tmp_path / "remote.db")
    collect(store)
    shift = ShiftApprovalService(store, check_approve=lambda c, p, plan: None, clock=lambda: NOW)
    with pytest.raises(EventError, match="version_conflict"):
        shift.approve(PRINCIPAL, PLAN, expected_version=2, expires_at=LATER, command_key="early")
    host = WorkflowHost(WorkflowRecovery(service(store, None), read_evidence=StageReaders(
        {s: producers.read for s in ("ANALYZE", "PROPOSE", "REHEARSE", "GATE")}
        | {"AWAIT_APPROVAL": shift_approval_reader})))
    host.sweep()
    producers.application = admit_change(store)
    host.sweep()
    version = store.snapshot(PLAN)["version"]
    too_long = (datetime.fromisoformat(NOW) + timedelta(hours=25)).isoformat()
    for expiry in (NOW, too_long):
        with pytest.raises(EventError, match="invalid_expiry"):
            shift.approve(PRINCIPAL, PLAN, expected_version=version, expires_at=expiry, command_key="x")
    first = shift.approve(PRINCIPAL, PLAN, expected_version=version, expires_at=LATER, command_key="k")
    assert shift.approve(PRINCIPAL, PLAN, expected_version=version, expires_at=LATER, command_key="k") == first
    with pytest.raises(EventError, match="command_key_conflict"):
        shift.approve(PRINCIPAL, PLAN, expected_version=version, expires_at=NOW + "x", command_key="k")


def test_rehearse_applies_the_approved_change_once_then_reads_it(host_case):  # noqa: F811
    f = host_case
    f.kube.cluster = "kubectl"
    with f.make() as host:
        stages = application_stages(host, None, receipts_dir=f.root, check_current=lambda *a: True,
                                    load_gate=lambda *a: pytest.fail("no gate on REHEARSE"),
                                    host_subject="workflow-host")
        assert set(stages) == {"REHEARSE"}
        version = f.backend.store.snapshot(PLAN)["version"]
        reader = stages["REHEARSE"](PLAN, PRINCIPAL.tenant_id, version)
        again = stages["REHEARSE"](PLAN, PRINCIPAL.tenant_id, version)  # replay: read only
    assert len(f.inputs) == 1
    with f.backend.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (PLAN,)).fetchone())
        proof = reader(c, plan, None, None)
        assert again(c, plan, None, None) == proof
    assert proof.result.deployed_digest == f.context.binding["image_digest"]


def test_rehearse_without_a_human_change_approval_holds(backend):
    from test_change import Fixture

    f = Fixture(backend)
    with pytest.raises(ValueError):
        application_stages(object(), None, receipts_dir="/", check_current=None, load_gate=None,
                           host_subject="workflow-host")
    from bravo_platform_events.workflow_stages import _approved_change

    assert _approved_change(f.backend.store, PLAN, f.backend.store.snapshot(PLAN)["version"]) is None


def test_every_stage_has_an_implementation():
    from bravo_platform_events import workflow_propose, workflow_stages

    wired = {"ANALYZE": "deployment port", "PROPOSE": workflow_propose.propose_preparer,
             "REHEARSE": workflow_stages.application_stages, "GATE": workflow_stages.application_stages,
             "AWAIT_APPROVAL": shift_approval_reader, "CUTOVER": integrity_reader, "WATCH": watch_reader}
    assert set(wired) == set(STAGES)


def test_operator_route_records_a_shift_approval(backend, tmp_path):
    from fastapi.testclient import TestClient

    from bravo_platform_events.cutover_api import create_cutover_app

    store = backend.store
    producers = Producers(tmp_path / "remote.db")
    collect(store)
    host = WorkflowHost(WorkflowRecovery(service(store, None), read_evidence=StageReaders(
        {s: producers.read for s in ("ANALYZE", "PROPOSE", "REHEARSE", "GATE")})))
    host.sweep()
    producers.application = admit_change(store)
    host.sweep()
    shift = ShiftApprovalService(store, check_approve=lambda c, p, plan: None, clock=lambda: NOW)
    app = create_cutover_app(service(store, None), resolve_principal=lambda request: PRINCIPAL, shift_service=shift)
    body = {"approval_kind": "shift", "expected_version": store.snapshot(PLAN)["version"], "expires_at": LATER}
    response = TestClient(app).post(f"/v1/plans/{PLAN}/approvals", json=body, headers={"Idempotency-Key": "s1"})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"approval_id", "approval_kind", "actor_id", "approved_at", "expires_at"}
    with store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (PLAN,)).fetchone())
        assert shift_approval_reader(c, plan, None, None).result.approval_id == response.json()["approval_id"]


def test_entry_point_builds_one_host_from_deployment_ports(backend, tmp_path):
    from bravo_platform_events.workflow_host_server import build

    producers = Producers(tmp_path / "remote.db")
    collect(backend.store)
    host = build(backend.store, lambda store: {"cutover": service(store, None),
                                               "readers": {"ANALYZE": producers.read}})
    assert host.sweep()[0].hold == "workflow_reader_unwired"  # PROPOSE not wired in these ports
    with pytest.raises(ValueError):
        build(backend.store, lambda store: {"cutover": service(store, None), "other": 1})


def test_host_applies_as_its_own_operator_identity_not_the_approver():
    from bravo_platform_events.operator_identity import OperatorPrincipal
    from bravo_platform_events.workflow_stages import host_principal

    p = host_principal("workflow-host", "tenant-demo", clock=lambda: 1000)
    assert isinstance(p, OperatorPrincipal) and p.subject_id == "workflow-host"
    assert (p.issued_at, p.expires_at, p.authenticated_at) == (1000, 1300, 1000)


def test_operator_access_requires_host_apply_grant_and_approver_grant(backend):
    """P1: OperatorAccess.change_authority accepts the host identity only with its own apply_change
    grant, and still re-checks the human approver's approve_change grant."""
    from test_cutover import EXPIRES

    from bravo_platform_events.operator_access import OperatorAccess
    from bravo_platform_events.operator_identity import OperatorPrincipal
    from bravo_platform_events.workflow_stages import host_principal

    collect(backend.store)
    access = OperatorAccess(backend.store, clock=lambda: NOW)
    now = int(datetime.fromisoformat(NOW).timestamp())
    host = host_principal("workflow-host", PRINCIPAL.tenant_id, clock=lambda: now)
    seen = []
    current = access.change_authority(lambda *a, **k: seen.append(k) or pytest.fail("upstream reached"))

    def call():
        with backend.store._transaction() as c:
            plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (PLAN,)).fetchone())
            current(c, host, plan, now=NOW, approved_by="approver", approval_id="appr-1")

    with pytest.raises(EventError, match="forbidden"):  # no host grant
        call()
    rev = 0
    for subject, permissions in (("workflow-host", ["read", "apply_change"]),):
        access.set_permissions(PLAN, OperatorPrincipal(subject, PRINCIPAL.tenant_id, now, now + 300, now, "s"),
                               permissions=permissions, valid_after=NOW, expires_at=EXPIRES,
                               expected_revision=rev, administration_ref="fixture-admin", command_key="grant-host")
    with pytest.raises(EventError, match="forbidden"):  # the approver has no approve_change grant
        call()


class _Delegations:
    """Stands in for ApprovalDelegations.principal_for (its own checks: tests/test_approval_delegation.py)."""

    def __init__(self, delegated):
        self.delegated, self.calls = delegated, []

    def principal_for(self, plan_id, gate, *, binding, version):
        self.calls.append((gate, binding, version))
        if not self.delegated:
            raise EventError("approval_not_delegated")
        return PRINCIPAL


@pytest.mark.parametrize("delegated", [True, False])
def test_a_delegated_shift_approval_is_recorded_by_the_host_once(backend, tmp_path, steps, delegated):
    from bravo_platform_events.workflow_stages import delegated_shift_preparer

    store = backend.store
    ledger = tmp_path / "remote.db"
    producers = Producers(ledger)
    reply = threading.Event()
    reply.set()
    with (remote_executor(ledger, threading.Event(), reply) as origin,
          ExecutorClient(origin, token="fixture-read", command_token="fixture-command") as client):
        shift = ShiftApprovalService(store, check_approve=lambda c, p, plan: None, clock=lambda: NOW)
        delegations = _Delegations(delegated)
        readers = StageReaders({"ANALYZE": producers.read, "PROPOSE": producers.read, "REHEARSE": producers.read,
                                "GATE": producers.read, "CUTOVER": integrity_reader},
                               preparers={"AWAIT_APPROVAL": delegated_shift_preparer(shift, delegations)})
        host = WorkflowHost(WorkflowRecovery(service(store, client), read_evidence=readers))
        collect(store)
        host.sweep()
        producers.application = admit_change(store)
        [step] = host.sweep()
        if delegated:
            assert step.after == "SHIFT"  # no person called the approval route
            [(gate, binding, version)] = delegations.calls
            assert gate == "shift" and binding == {"plan_version": str(version)}
        else:
            assert (step.after, step.hold) == ("AWAIT_APPROVAL", "shift_approval_required")


def test_a_delegated_change_approval_approves_the_current_candidate_as_the_approver(backend, keys):  # noqa: F811
    from test_change_intake import BINDING as CANDIDATE_BINDING
    from test_change_intake import NORMAL, IntakeFixture

    from bravo_platform_events.workflow_stages import _delegated_change

    f = IntakeFixture(backend, keys[0])
    plan_id = NORMAL.plan_id
    assert _delegated_change(f.service(), _Delegations(True), plan_id, 1) is None  # no candidate yet
    f.deliver()
    assert _delegated_change(f.service(), _Delegations(False), plan_id, 1) is None  # a person decides
    delegations = _Delegations(True)
    receipt = _delegated_change(f.service(), delegations, plan_id, 1)
    assert receipt["actor_id"] == PRINCIPAL.subject_id and receipt["run_id"] == NORMAL.run_id
    assert receipt["binding"] == CANDIDATE_BINDING and delegations.calls == [("change", CANDIDATE_BINDING, 1)]
    assert _delegated_change(f.service(), delegations, plan_id, 1) == receipt  # same decision, once
    f.hold()
    assert _delegated_change(f.service(), delegations, plan_id, 1) is None  # a held candidate is never approved
