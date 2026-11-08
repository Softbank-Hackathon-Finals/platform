"""T10 admission with real plan transactions and a simulated external T15 worker."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_change_intake import BINDING, NORMAL, PLAN, SOURCE, IntakeFixture
from test_cutover import BINDINGS, EXPIRES, NOW, PRINCIPAL
from test_operator_access import Identity

from bravo_platform_events.change import ChangeApprovalService, CurrentChange
from bravo_platform_events.cutover import CutoverService
from bravo_platform_events.operator_access import OperatorAccess, create_operator_cutover_app
from bravo_platform_events.rehearsal import (
    Observation,
    RehearsalService,
    RehearsalTask,
    require_rehearsals_complete,
)
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import WorkflowRecovery


class Worker:
    def __init__(self):
        self.effects = []
        self.results = {}
        self.lose_ack = False

    def task(self, connection, plan, task_id):
        if task_id not in ("bucket", "secret"):
            raise EventError("task_unavailable")
        return RehearsalTask("a" * 40, ("b" if task_id == "bucket" else "c") * 64)

    def submit(self, admission):
        self.effects.append(admission)
        if self.lose_ack:
            raise TimeoutError("worker accepted the attempt, then its reply was lost")

    def observe(self, admission):
        return self.results.get((admission.rehearsal_id, admission.attempt))


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Scenario:
    def __init__(self, backend, key):
        self.backend = backend
        self.proofs = IntakeFixture(backend, key)
        self.worker = Worker()
        self.allowed = True

    def access(self, connection, principal, plan):
        if not self.allowed:
            raise EventError("forbidden")

    def service(self):
        return RehearsalService(self.backend.reopen(), host=self.worker, check_start=self.access)

    def start(self, task="bucket", attempt=1, key="start-bucket", **kwargs):
        return self.service().start(PRINCIPAL, PLAN.plan_id, **(dict(task_id=task, expected_version=1,
                                         attempt=attempt, command_key=key) | kwargs))

    def reconcile(self, attempt):
        return self.service().reconcile(PRINCIPAL, PLAN.plan_id,
                                        rehearsal_id=attempt.rehearsal_id, attempt=attempt.attempt)

    def guard(self):
        with self.backend.store._transaction(write=True, plan_id=PLAN.plan_id) as c:
            plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (PLAN.plan_id,)).fetchone())
            require_rehearsals_complete(c, plan)

    def result(self, attempt, verdict, **changes):
        result = Observation(attempt, NORMAL.verification_id, NORMAL.proposal_digest, NORMAL.namespace,
                             SOURCE.created_uid, SOURCE.removed_uid, True, verdict)
        self.worker.results[(attempt.rehearsal_id, attempt.attempt)] = replace(result, **changes)

    def pass_proof(self, attempt, verification_id=NORMAL.verification_id):
        candidate = replace(NORMAL, run_id=attempt.rehearsal_id, attempt=attempt.attempt,
                            verification_id=verification_id)
        self.proofs.original = replace(SOURCE, candidate=candidate)
        self.proofs.deliver(candidate)


@pytest.fixture
def scenario(backend, signing_key):
    return Scenario(backend, signing_key)


def test_lost_ack_reopen_and_parallel_replay_never_resubmit(scenario):
    s = scenario
    s.worker.lose_ack = True
    with pytest.raises(EventError, match="rehearsal_submission_unknown"):
        s.start()
    result = s.start()
    attempt, = s.worker.effects
    assert result["rehearsal_id"] == attempt.rehearsal_id
    assert (result["attempt"], result["task_id"], result["plan_version"]) == (1, "bucket", 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(r["rehearsal_id"] == attempt.rehearsal_id for r in pool.map(lambda _: s.start(), range(8)))
    assert s.reconcile(attempt)["status"] == "UNCONFIRMED"
    cutover = CutoverService(s.backend.store, object(), bindings=BINDINGS)
    with pytest.raises(EventError, match="rehearsal_incomplete"):
        WorkflowRecovery(cutover).resume_once(PRINCIPAL, PLAN.plan_id)
    with pytest.raises(EventError):
        s.start(attempt=2, key="second-attempt")
    assert s.worker.effects == [attempt]


def test_same_task_concurrent_keys_create_one_logical_run_and_rollback_creates_none(scenario):
    s = scenario
    with s.backend.reject_insert("plan_commands"), pytest.raises(s.backend.storage_error):
        s.start()
    assert not s.worker.effects

    barrier = Barrier(2)
    def start(key):
        barrier.wait(5)
        try:
            return s.start(key=key)["rehearsal_id"]
        except EventError:
            return None  # A conflicting caller may be rejected instead of receiving the original.

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(start, ("start-a", "start-b")))
    attempt, = s.worker.effects
    assert set(ids) - {None} == {attempt.rehearsal_id}
    with pytest.raises(EventError):
        s.start(task="unsupported", key="unknown-task")
    assert s.worker.effects == [attempt]


def test_plan_state_and_current_authority_precede_dispatch_and_replay(scenario):
    s = scenario
    s.allowed = False
    with pytest.raises(EventError, match="forbidden"):
        s.start()
    assert not s.worker.effects
    s.allowed = True
    with pytest.raises(EventError):
        s.start(expected_version=2)
    entered, release = Event(), Event()

    def move_plan():
        with s.backend.store._transaction(write=True, plan_id=PLAN.plan_id) as c:
            entered.set()
            assert release.wait(8)
            c.execute("UPDATE plans SET state='GATE', version=2 WHERE plan_id=?", (PLAN.plan_id,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        transition = pool.submit(move_plan)
        assert entered.wait(8)
        start = pool.submit(s.start)
        release.set()
        transition.result(timeout=8)
        with pytest.raises(EventError):
            start.result(timeout=8)
    assert not s.worker.effects


def test_verified_pass_only_unblocks_after_every_started_task_finishes(scenario):
    s = scenario
    s.start()
    s.start("secret", key="start-secret")
    first, second = s.worker.effects
    assert first.rehearsal_id != second.rehearsal_id
    for n, attempt in enumerate((first, second), 1):
        verification_id = f"verified-{n}"
        s.result(attempt, "PASS", verification_id=verification_id)
        # A worker PASS without the original candidate cannot release the plan.
        with pytest.raises(EventError, match="rehearsal_verification_unavailable"):
            s.reconcile(attempt)
        with pytest.raises(EventError):
            s.guard()
        s.pass_proof(attempt, verification_id)
        assert s.reconcile(attempt)["status"] == "PASS"
        if n == 1:
            with pytest.raises(EventError):
                s.guard()
    s.guard()
    s.allowed = False
    with pytest.raises(EventError, match="forbidden"):
        s.start()
    assert s.worker.effects == [first, second]


def test_only_clean_failure_with_its_hold_can_retry_and_old_results_cannot_finish_new_attempt(scenario):
    s = scenario
    s.start()
    first, = s.worker.effects
    s.result(first, "FAIL", removed_uid="replacement")
    with pytest.raises(EventError, match="rehearsal_observation_mismatch"):
        s.reconcile(first)
    with pytest.raises(EventError):
        s.start(attempt=2, key="retry")
    s.result(first, "FAIL")
    assert s.reconcile(first)["status"] == "FAIL"
    with pytest.raises(EventError):
        s.start(attempt=2, key="retry")
    s.proofs.hold()
    second_result = s.start(attempt=2, key="retry")
    _, second = s.worker.effects
    assert second_result["rehearsal_id"] == first.rehearsal_id == second.rehearsal_id
    assert s.start(attempt=2, key="retry")["attempt"] == 2
    s.worker.results[(second.rehearsal_id, 2)] = s.worker.results[(first.rehearsal_id, 1)]
    with pytest.raises(EventError, match="rehearsal_observation_mismatch"):
        s.reconcile(second)
    with pytest.raises(EventError):
        s.guard()
    assert s.worker.effects == [first, second]


def test_three_confirmed_attempts_exhaust_run_even_after_reopening(scenario):
    s = scenario
    allowed_ids = {f"failure-{n}" for n in (1, 2, 3)}
    s.proofs.intake.authorize_hold = (
        lambda _c, principal, plan, vid, decision:
        principal == "admin-fixture" and plan == PLAN.plan_id and vid in allowed_ids and decision == "stop"
    )
    for n in (1, 2, 3):
        verification_id = f"failure-{n}"
        s.start(attempt=n, key=f"try-{n}")
        attempt = s.worker.effects[-1]
        assert attempt.attempt == n
        s.result(attempt, "FAIL", verification_id=verification_id)
        assert s.reconcile(attempt)["status"] == "FAIL"
        s.proofs.intake.hold("admin-fixture", PLAN.plan_id, verification_id, decision_ref="stop")
    assert len({x.rehearsal_id for x in s.worker.effects}) == 1
    with pytest.raises(ValueError, match="invalid_rehearsal_attempt"):
        s.start(attempt=4, key="fourth")
    with pytest.raises(EventError):
        s.guard()
    assert [a.attempt for a in s.worker.effects] == [1, 2, 3]


def test_hold_committed_after_pass_blocks_gate(scenario):
    s = scenario
    s.start()
    attempt, = s.worker.effects
    s.result(attempt, "PASS")
    s.pass_proof(attempt)
    assert s.reconcile(attempt)["status"] == "PASS"
    s.guard()
    s.proofs.hold()
    with pytest.raises(EventError, match="verification_held"):
        s.guard()


def test_managed_claim_must_wait_for_this_attempt_and_reject_unadmitted_future_proof(scenario):
    s = scenario
    s.start()
    first, = s.worker.effects
    s.pass_proof(first)
    with pytest.raises(EventError):
        s.guard()
    def approve():
        return s.proofs.service().approve(PRINCIPAL, PLAN.plan_id, run_id=first.rehearsal_id,
                                          binding=BINDING, expected_version=1, command_key="approve-first")
    with pytest.raises(EventError):
        approve()
    with pytest.raises(EventError):
        s.pass_proof(replace(first, attempt=2), "future-verification")
    s.result(first, "PASS")
    assert s.reconcile(first)["status"] == "PASS"
    s.guard()
    approval = approve()
    application, created = s.proofs.claim(approval)
    assert created and application["rehearsal_id"] == first.rehearsal_id


def test_legacy_candidate_preserves_its_approval_when_managed_start_is_rejected(scenario):
    s = scenario
    original = s.proofs.deliver()
    with pytest.raises(EventError):
        s.start()
    assert not s.worker.effects
    assert s.proofs.deliver() == original
    approved = s.proofs.approve()
    receipt, created = s.proofs.claim(approved)
    assert created and receipt["verification_id"] == NORMAL.verification_id


def test_managed_plan_rejects_new_legacy_delivery_and_approval_from_custom_authority(scenario):
    s = scenario
    s.start()
    attempt, = s.worker.effects
    with pytest.raises(EventError):
        s.proofs.deliver()  # Authenticated proof whose run never got a slot.
    def candidate(_c, _principal, _plan, *, rehearsal_id, **_kwargs):
        return CurrentChange(BINDING, rehearsal_id, "unconfirmed", "provider-revision")
    service = ChangeApprovalService(s.backend.store, authority=candidate, clock=lambda: NOW)
    for run_id in (NORMAL.run_id, attempt.rehearsal_id):
        with pytest.raises(EventError):
            service.approve(PRINCIPAL, PLAN.plan_id, run_id=run_id, binding=BINDING, expected_version=1,
                            command_key="unconfirmed-" + run_id)


def test_existing_custom_approval_excludes_new_managed_admission(scenario):
    s = scenario
    def candidate(_c, _principal, _plan, *, rehearsal_id, **_kwargs):
        return CurrentChange(BINDING, rehearsal_id, "legacy-verification", "provider-revision")
    service = ChangeApprovalService(s.backend.store, authority=candidate, clock=lambda: NOW)
    approved = service.approve(PRINCIPAL, PLAN.plan_id, run_id=NORMAL.run_id, binding=BINDING,
                               expected_version=1, command_key="legacy-approval")
    with pytest.raises(EventError):
        s.start()
    assert not s.worker.effects
    receipt, created = service.claim_application(PRINCIPAL, PLAN.plan_id,
                                                approval_id=approved["approval_id"], command_key="apply-legacy")
    assert created and receipt["verification_id"] == "legacy-verification"


@pytest.mark.parametrize("changes", [
    {"image_digest": "sha256:" + "e" * 64},
    {"expires_at": "2026-11-03T10:15:00+00:00"},
])
def test_same_verified_id_cannot_substitute_a_different_approved_artifact_or_lifetime(scenario, changes):
    s = scenario
    s.start()
    attempt, = s.worker.effects
    s.pass_proof(attempt)
    s.result(attempt, "PASS")
    assert s.reconcile(attempt)["status"] == "PASS"
    binding = BINDING | changes
    def altered(_c, _principal, _plan, **_kwargs):
        return CurrentChange(binding, attempt.rehearsal_id, NORMAL.verification_id, "custom-revision")
    service = ChangeApprovalService(s.backend.store, authority=altered, clock=lambda: NOW)
    with pytest.raises(EventError):
        service.approve(PRINCIPAL, PLAN.plan_id, run_id=attempt.rehearsal_id, binding=binding,
                        expected_version=1, command_key="try-approval")
    approved = s.proofs.service().approve(PRINCIPAL, PLAN.plan_id, run_id=attempt.rehearsal_id,
                                         binding=BINDING, expected_version=1, command_key="try-approval")
    assert approved["binding"] == BINDING


def test_http_admission_requires_live_plan_specific_grant_and_idempotency_key(scenario, signing_key):
    s = scenario
    identity = Identity(signing_key)
    access = OperatorAccess(s.backend.store, clock=lambda: NOW)
    app = create_operator_cutover_app(s.backend.store, object(), bindings=BINDINGS,
        verifier=identity.verifier, rehearsal_host=s.worker, clock=lambda: NOW)
    client = TestClient(app)
    path = f"/v1/plans/{PLAN.plan_id}/rehearsals"
    body = dict(task_id="bucket", expected_version=1, attempt=1)
    authorization = {"Authorization": "Bearer " + identity.token()}
    headers = authorization | {"Idempotency-Key": "http-start"}

    def grant(permissions, current):
        access.set_permissions(PLAN.plan_id, identity.principal(), permissions=permissions, valid_after=NOW,
            expires_at=EXPIRES, expected_revision=current, administration_ref="operator-admin",
            command_key=f"access-{current}")

    grant(["read"], 0)
    assert client.post(path, json=body, headers=headers).status_code == 403
    grant(["read", "start_rehearsal"], 1)
    assert client.post(path, json=body, headers=authorization).status_code == 422
    assert not s.worker.effects
    start = client.post(path, json=body, headers=headers)
    assert start.status_code == 202
    attempt, = s.worker.effects
    assert start.json()["rehearsal_id"] == attempt.rehearsal_id
    reconcile = f"{path}/{attempt.rehearsal_id}/attempts/1/reconcile"
    assert client.post(reconcile, headers=authorization).json()["status"] == "UNCONFIRMED"
    assert client.post(path, json=body | {"task_id": "secret"}, headers=headers).status_code == 409
    grant(["read"], 2)
    assert client.post(path, json=body, headers=headers).status_code == 403
    assert client.post(reconcile, headers=authorization).status_code == 403
    assert s.worker.effects == [attempt]


def test_malformed_authority_is_not_a_client_error(scenario, monkeypatch):
    s = scenario
    with monkeypatch.context() as patch:
        patch.setattr(s.worker, "task", lambda *_: RehearsalTask(None, "bad"))
        with pytest.raises(EventError, match="authority_unavailable"):
            s.start()
    assert not s.worker.effects
    s.start()
    admission, = s.worker.effects
    s.result(admission, "FAIL", proposal_digest="bad")
    with pytest.raises(EventError, match="rehearsal_observation_mismatch"):
        s.reconcile(admission)
    with pytest.raises(EventError, match="rehearsal_incomplete"):
        s.guard()


def test_demo_mode_passes_rehearse_and_gate_on_the_proposed_change(scenario):
    # T49 demo: no T15 run, no change approval, no T19 recheck; the PROPOSE change carries through.
    from bravo_platform_events.cutover import _get, _put
    from bravo_platform_events.workflow import demo_reader

    s = scenario
    change = {"diff_hash": "sha256:" + "a" * 64, "image_digest": "sha256:" + "b" * 64}
    with s.backend.store._transaction(write=True, plan_id=PLAN.plan_id) as c:
        version = c.execute("SELECT version FROM plans WHERE plan_id=?", (PLAN.plan_id,)).fetchone()["version"]
        _put(c, PLAN.plan_id, f"workflow:{version - 1}", {}, {"result": {"change": change}})
    cutover = CutoverService(s.backend.store, object(), bindings=BINDINGS)
    with pytest.raises(EventError, match="workflow_condition_unmet"):  # outside demo mode it is still checked
        WorkflowRecovery(cutover, read_evidence=demo_reader("REHEARSE")).resume_once(PRINCIPAL, PLAN.plan_id)

    def read(c, plan, previous, run):
        return demo_reader(plan["state"])(c, plan, previous, run)

    demo = WorkflowRecovery(cutover, read_evidence=read, demo_skip={"REHEARSE", "GATE"})
    assert demo.resume_once(PRINCIPAL, PLAN.plan_id) == "GATE"
    assert demo.resume_once(PRINCIPAL, PLAN.plan_id) == "AWAIT_APPROVAL"
    with s.backend.store._transaction() as c:
        gate = _get(c, PLAN.plan_id, f"workflow:{version + 1}")[1]["result"]
    assert gate["verdict"] == "PASS" and gate["change"] == change
    with pytest.raises(ValueError):
        demo_reader("CUTOVER")


def test_demo_watch_reader_passes_on_the_cutover_run():
    from bravo_platform_events.workflow import Run, Watch, demo_reader

    previous = {"receipt_id": "integrity-1", "result": {"run": {"command_id": "c", "epoch": 2, "execution_id": "e"}}}
    plan = {"plan_id": PLAN.plan_id, "version": 9, "spec_json": '{"plan_digest": "d", "target_environment_id": "t"}'}
    proof = demo_reader("WATCH")(None, plan, previous, None)
    assert proof.result == Watch(Run("c", 2, "e"), "integrity-1", "PASS", "PASS") and proof.version == 9
