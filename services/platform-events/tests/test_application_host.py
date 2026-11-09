"""T10 transactions plus the real T31 library; cluster/candidate are synthetic."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bravo_apply.binding import diff_hash, render_diff
from bravo_apply.deploy import FakeKube
from test_change import Fixture
from test_cutover import PRINCIPAL, SPEC

from bravo_platform_events.application_host import (
    ApplicationEvidence,
    ApplicationHost,
    ApplyInput,
    _post_apply,
)
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.store import EventError


@pytest.fixture
def host_case(backend, tmp_path):
    f = Fixture(backend)
    # Expiry is a synthetic test clock interval, not an operational approval.
    f.now = datetime.now(UTC).isoformat()
    before = "old\n"
    f.proposal = dict(run_id="rehearsal-1", edits=[dict(path="app.py",
        before_sha256=hashlib.sha256(before.encode()).hexdigest(), replacement="new\n")])
    f.diff = render_diff({"app.py": before}, f.proposal["edits"])
    f.context = replace(f.context, binding=f.context.binding | {
        "diff_hash": diff_hash(f.diff), "expires_at": "2099-01-01T00:00:00Z"})
    f.root = tmp_path / "receipts"
    f.root.mkdir()
    f.kube = FakeKube("ghcr.io/example/demo-chat@sha256:" + "0" * 64)
    f.inputs = []
    f.input = ApplyInput(f.proposal, f.diff, SPEC.target_environment_id,
                        "target-dev", "demo-chat", "app", "ghcr.io/example/demo-chat")

    def load(c, principal, plan, claim):
        f.inputs.append(claim["application_id"])
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone()
        return f.input

    f.load = load
    f.service = lambda: ChangeApprovalService(f.backend.reopen(), authority=f.authority,
        check_access=lambda c, p, plan: None, clock=lambda: f.now)
    f.approved = f.approve(binding=f.context.binding)
    f.make = lambda: ApplicationHost(f.service(), kube=f.kube, receipts_dir=f.root, load_input=f.load)
    f.start = lambda host: host.start(PRINCIPAL, SPEC.plan_id,
        approval_id=f.approved["approval_id"], command_key="host-application")
    return f


def test_new_admission_once_retry_and_process_reopen_read_only(host_case):
    f = host_case
    with f.make() as host:
        result = f.start(host)
        assert result.outcome == "APPLIED" and result.cluster == "fake"
        assert f.start(host) == result
    f.load = lambda *args: pytest.fail("retry loaded candidate")
    with f.make() as reopened:
        assert f.start(reopened) == result
    assert len(f.kube.patches) == len(f.inputs) == 1
    assert f.backend.reopen().snapshot(SPEC.plan_id)["state"] == "REHEARSE"


def test_claim_before_record_crash_never_dispatches_on_retry(host_case):
    f = host_case
    def die(*args):
        raise SystemExit("synthetic crash after durable claim")
    f.load = die
    with f.make() as host, pytest.raises(SystemExit):
        f.start(host)
    f.load = lambda *args: pytest.fail("claimed application was redispatched")
    with f.make() as reopened:
        assert f.start(reopened) is None
    assert f.kube.patches == []


def test_started_record_survives_crash_without_second_patch(host_case):
    f = host_case
    original = f.kube.patch_image
    def crash(*args):
        original(*args)
        raise SystemExit("synthetic crash after effect")
    f.kube.patch_image = crash
    with f.make() as host, pytest.raises(SystemExit):
        f.start(host)
    with f.make() as reopened:
        assert f.start(reopened).outcome == "UNKNOWN"
    assert len(f.kube.patches) == 1


def test_concurrent_process_owner_and_missing_mount_refused(host_case, tmp_path):
    f = host_case
    with f.make():
        with pytest.raises(BlockingIOError):
            f.make()
    with pytest.raises(ValueError, match="persistent_receipts_mount_required"):
        ApplicationHost(f.service(), kube=f.kube, receipts_dir=tmp_path / "missing", load_input=f.load)
    assert not (tmp_path / "missing").exists()


def test_current_authority_revoked_between_claim_and_effect(host_case):
    f = host_case
    base = f.service
    def changed_service():
        service = base()
        claim = service.claim_application
        def revoke(*args, **kwargs):
            result = claim(*args, **kwargs)
            f.allowed = False
            return result
        service.claim_application = revoke
        return service
    f.service = changed_service
    with f.make() as host, pytest.raises(EventError, match="authority_unavailable"):
        f.start(host)
    assert f.kube.patches == []


def test_wrong_registered_target_cannot_use_approved_claim(host_case):
    f = host_case
    f.input = replace(f.input, target_environment_id="other")
    with f.make() as host, pytest.raises(EventError, match="application_input_scope_mismatch"):
        f.start(host)
    assert f.kube.patches == []


def test_changed_candidate_refused_and_not_retried(host_case):
    f = host_case
    f.input = replace(f.input, diff=b"unapproved\n")
    with f.make() as host:
        result = f.start(host)
        assert result.outcome == "REFUSED"
        assert f.start(host) == result
    assert f.kube.patches == []


def test_evidence_rejects_fake_and_current_revocation(host_case):
    f = host_case
    with f.make() as host:
        result = f.start(host)
    reader = ApplicationEvidence(principal=PRINCIPAL, application_id=result.application_id,
        receipts_dir=f.root, records=None, check_current=lambda *a: True,
        load_gate=lambda *a: pytest.fail("gate on REHEARSE"), fallback=lambda *a: None)
    with f.backend.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        with pytest.raises(EventError, match="real_application_required"):
            reader(c, plan, None, None)


def test_application_evidence_binds_claim_and_current_authority(host_case):
    f = host_case
    # Simulate the production adapter marker only within this unit test.
    # This test does not perform Kubernetes I/O or provide runtime evidence.
    f.kube.cluster = "kubectl"
    with f.make() as host:
        result = f.start(host)
    allowed = [True]
    reader = ApplicationEvidence(principal=PRINCIPAL, application_id=result.application_id,
        receipts_dir=f.root, records=None, check_current=lambda *a: allowed[0],
        load_gate=lambda *a: pytest.fail("gate on REHEARSE"), fallback=lambda *a: None)
    with f.backend.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
        proof = reader(c, plan, None, None)
        assert proof.result.application_id == result.application_id
        assert proof.result.deployed_digest == f.context.binding["image_digest"]
        allowed[0] = False
        with pytest.raises(EventError, match="application_authority_unavailable"):
            reader(c, plan, None, None)


@pytest.mark.parametrize("fault", [None, "profile", "before", "missing", "stale_evidence", "phase", "application"])
def test_post_apply_recheck_needs_current_target_and_all_requested_controls(fault):
    from bravo_apply.path import GATE_RECHECK_TESTS

    ids = list(GATE_RECHECK_TESTS)
    rows = [dict(test_id=t, evidence=dict(collected_at="2026-11-06T01:01:00Z")) for t in ids]
    document = dict(started_at="2026-11-06T01:01:00Z", results=rows)
    if fault == "profile":
        ids.remove("ENC-01.1")
    elif fault == "before":
        document["started_at"] = "2026-11-06T00:59:00Z"
    elif fault == "missing":
        rows.pop()
    elif fault == "stale_evidence":
        rows[0]["evidence"]["collected_at"] = "2026-11-06T00:59:00Z"
    view = SimpleNamespace(principal=PRINCIPAL,
        gate_phase="initial" if fault == "phase" else "post_apply",
        application_id="other" if fault == "application" else "applied-1",
        context=SimpleNamespace(profile_id="fixture", profile_version="1"),
        records=SimpleNamespace(contract=SimpleNamespace(profile=lambda *a: dict(required_test_ids=ids))),
        run_provider=lambda *a: SimpleNamespace(document=document))
    outcome = SimpleNamespace(application_id="applied-1", finished_at="2026-11-06T01:00:00Z")
    if fault:
        with pytest.raises(EventError):
            _post_apply(view, outcome)
    else:
        _post_apply(view, outcome)
