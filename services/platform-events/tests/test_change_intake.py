"""Synthetic T15 examples, real signatures, real plan transactions; no product verifier."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_cutover import EXPIRES, NOW, PRINCIPAL, SPEC

from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.change_intake import Candidate, ChangeIntake, SourceVerification
from bravo_platform_events.store import EventError
from bravo_platform_events.workload_identity import WorkloadSender

PLAN = replace(SPEC, plan_id="plan-fixture")
NORMAL = Candidate(PLAN.plan_id, "run-fixture", 1, "verification-fixture-1", "evidence-fixture-1",
                   "env-onprem", "rehearsal-fixture", "b" * 64, "sha256:" + "c" * 64, "sha256:" + "d" * 64)
SOURCE = SourceVerification(NORMAL, PLAN.tenant_id, PLAN.plan_digest, 1, EXPIRES, "PASS",
                            "fixture-uid-1", "fixture-uid-1", True)
BINDING = dict(plan_id=PLAN.plan_id, plan_digest=PLAN.plan_digest, expires_at=EXPIRES,
               target_environment_id=NORMAL.target_environment_id, diff_hash=NORMAL.diff_hash,
               image_digest=NORMAL.image_digest)


@pytest.fixture(scope="module")
def keys():
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


def signed(key, **overrides):
    now = int(time.time())
    return jwt.encode(dict(iss="https://issuer.invalid", aud=["t10-fixture"], sub="fixture-sa",
                           iat=now, exp=now+120) | overrides, key, algorithm="RS256")


def sender(key):
    return WorkloadSender("https://issuer.invalid", "t10-fixture", "fixture-sa", key.public_key(), PLAN.tenant_id)


@pytest.mark.parametrize("fault", ["signature", "iss", "aud", "extra-audience", "sub", "exp", "long", "no-token"])
def test_wrong_sender_is_rejected(keys, fault):
    overrides = {"iss": {"iss": "https://other.invalid"}, "aud": {"aud": "human"}, "sub": {"sub": "other-sa"},
                 "extra-audience": {"aud": ["t10-fixture", "extra"]}, "exp": {"exp": 1},
                 "long": {"exp": int(time.time()) + 7200}}.get(fault, {})
    token = signed(keys[int(fault == "signature")], **overrides) if fault != "no-token" else ""
    with pytest.raises(EventError, match="forbidden"):
        sender(keys[0]).authenticate(token)


class IntakeFixture:
    def __init__(self, backend, key):
        self.backend = backend
        self.original = SOURCE
        self.token = signed(key)
        self.reads = []
        backend.store.register_plan(PLAN, run_id="fixture", occurred_at=NOW)
        with backend.admin() as c:
            c.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id='plan-fixture'")
        def read(*refs):
            self.reads.append(refs)
            return self.original
        self.intake = ChangeIntake(backend.store, sender=sender(key), read_original=read,
            authorize_hold=lambda _c, identity, plan, verification, ref:
                (identity, plan, verification, ref) == ("fixture-admin", PLAN.plan_id, NORMAL.verification_id, "decision-1"),
            clock=lambda: NOW)

    def deliver(self, candidate=NORMAL):
        return self.intake.receive(self.token, candidate)

    def hold(self, identity="fixture-admin"):
        return self.intake.hold(identity, PLAN.plan_id, NORMAL.verification_id, decision_ref="decision-1")

    def service(self):
        return ChangeApprovalService(self.backend.reopen(), authority=self.intake.current, clock=lambda: NOW)

    def approve(self, key="approve"):
        return self.service().approve(PRINCIPAL, PLAN.plan_id, run_id=NORMAL.run_id, binding=BINDING,
                                      expected_version=1, command_key=key)

    def claim(self, approval, key="claim"):
        return self.service().claim_application(PRINCIPAL, PLAN.plan_id, approval_id=approval["approval_id"],
                                                command_key=key)


@pytest.fixture
def f(backend, keys):
    return IntakeFixture(backend, keys[0])


def test_hold_after_claim_returns_only_saved_application(f):
    received = f.deliver()
    assert received == f.deliver()
    assert f.reads == [(PLAN.plan_id, NORMAL.run_id, NORMAL.verification_id, NORMAL.evidence_ref)] * 2
    approval = f.approve()
    first, created = f.claim(approval)
    assert created and first["verification_id"] == NORMAL.verification_id
    assert first["context_revision"] == received["revision"]
    held = f.hold()
    assert held == f.hold() and held["revision"] != received["revision"]
    assert f.claim(approval) == (first, False)
    with pytest.raises(EventError, match="approval_unavailable"):
        f.claim(approval, "second-claim")
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve("second-approval")
    with pytest.raises(EventError, match="verification_held"):
        f.deliver()


def test_hold_during_evidence_fetch_wins_even_before_first_delivery(f):
    entered, resume = Event(), Event()
    def blocked(*refs):
        entered.set()
        assert resume.wait(10)
        return SOURCE
    f.intake.read_original = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(f.deliver)
        try:
            assert entered.wait(10)
            f.hold()
        finally:
            resume.set()
        with pytest.raises(EventError, match="verification_held"):
            late.result(timeout=10)
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()


def test_hold_commits_while_approved_claim_is_waiting_for_same_lock(f):
    f.deliver()
    approval = f.approve()
    entered, release, waiting = Event(), Event(), Event()
    verify = f.intake.authorize_hold
    def pause_under_lock(*args):
        entered.set()
        assert release.wait(10)
        return verify(*args)
    f.intake.authorize_hold = pause_under_lock
    def claim():
        waiting.set()
        return f.claim(approval)
    with ThreadPoolExecutor(max_workers=2) as pool:
        hold = pool.submit(f.hold)
        try:
            assert entered.wait(10)
            pending = pool.submit(claim)
            assert waiting.wait(10)
        finally:
            release.set()
        hold.result(timeout=10)
        with pytest.raises(EventError, match="authority_unavailable"):
            pending.result(timeout=10)


def test_replay_cannot_restore_superseded_attempt_or_mutate_verification(f):
    original = f.deliver()
    new = replace(NORMAL, verification_id="v2", attempt=2)
    f.original = replace(SOURCE, candidate=new)
    assert f.deliver(new)["revision"] != original["revision"]
    f.original = SOURCE
    assert f.deliver() == original
    assert f.approve()["context"]["verification_id"] == "v2"
    for changed, error in [(replace(NORMAL, attempt=3), "verification_conflict"),
                           (replace(NORMAL, verification_id="late"), "attempt_conflict")]:
        f.original = replace(SOURCE, candidate=changed)
        with pytest.raises(EventError, match=error):
            f.deliver(changed)


def test_token_does_not_authorize_hold_or_unverified_source(f):
    with pytest.raises(EventError, match="forbidden"):
        f.intake.receive("unverified", NORMAL)
    assert not f.reads
    with pytest.raises(EventError, match="forbidden"):
        f.hold(f.token)
    for faulty in [None, replace(SOURCE, verdict="UNKNOWN"), replace(SOURCE, absence_confirmed=False),
                   replace(SOURCE, removed_uid="another-uid"), replace(SOURCE, tenant_id="another-tenant"),
                   replace(SOURCE, candidate=replace(NORMAL, image_digest="sha256:" + "e" * 64)),
                   replace(SOURCE, plan_digest="c" * 64)]:
        f.original = faulty
        with pytest.raises(EventError):
            f.deliver()
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()


def test_previous_plan_version_cannot_issue_fresh_approval_or_be_registered_late(f):
    f.deliver()
    old = f.approve()
    # A different workflow cycle returns to REHEARSE with the same spec digest.
    with f.backend.admin() as c:
        c.execute("UPDATE plans SET version=3 WHERE plan_id='plan-fixture'")
    with pytest.raises(EventError, match="authority_unavailable"):
        f.service().approve(PRINCIPAL, PLAN.plan_id, run_id=NORMAL.run_id, binding=BINDING,
                            expected_version=3, command_key="new-cycle")
    with pytest.raises(EventError):
        f.claim(old)
    late = replace(NORMAL, verification_id="late-proof", attempt=2)
    f.original = replace(SOURCE, candidate=late)
    with pytest.raises(EventError, match="version_conflict"):
        f.deliver(late)
    # Evidence tied to the new version can still proceed.
    f.original = replace(SOURCE, candidate=late, plan_version=3)
    f.deliver(late)
    approved = f.service().approve(PRINCIPAL, PLAN.plan_id, run_id=NORMAL.run_id, binding=BINDING,
                                   expected_version=3, command_key="new-cycle")
    assert approved["context"]["verification_id"] == "late-proof"
