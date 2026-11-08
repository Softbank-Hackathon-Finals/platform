"""Security reviews are fresh conditions; platform authority remains the executor gate."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sova_security.adapters.modernization_codec import decode, encode
from sova_security.contracts.commands import ContractError, ResourceScope
from sova_security.contracts.evidence import (
    EvidenceRecord,
    Gate,
    RevisionSet,
    VerificationObligation,
    VerificationResult,
)
from sova_security.domain.review_service import RemoteProgramReview, ReviewRegistration
from sova_security.domain.security_program import (
    ProgramBinding,
    ProgramKind,
    ProgramPhase,
    SecurityProgram,
    checks_for,
    obligation_id,
    review_program,
)
from test_gate_cutover_phase import flow  # noqa: F401
from test_gate_records import bundle, contract, gate_pg, ready, records, service  # noqa: F401

from bravo_platform_events.change import CurrentChange
from bravo_platform_events.security_governance import (
    SecurityProgramRecords,
    SecurityReviewAuthority,
    _host_binding,
    review_ports,
)
from bravo_platform_events.security_relay import _access_clock, probe
from bravo_platform_events.store import EventError

BASELINE = "7" * 40


def test_security_relay_adapts_server_clock_for_operator_access():
    now = datetime(2026, 11, 8, 2, 0, tzinfo=UTC)
    assert _access_clock(lambda: now)() == "2026-11-08T02:00:00+00:00"


def _bundle(f, kind):
    context, now = f.state["context"], f.state["now"]
    target = ResourceScope(context.tenant_id, context.app, context.target_environment_id, "workload")
    source = replace(target, environment_id=context.source_environment_id)
    revisions = RevisionSet("b" * 64, "c" * 64, "d" * 64, "e" * 64)
    bindings = tuple(ProgramBinding(check.check_id,
        VerificationObligation(obligation_id(context.plan_digest, check.check_id),
            source if check.role == "source" else target,
            Gate.PREFLIGHT if check.phase == ProgramPhase.PREFLIGHT else Gate.POSTFLIGHT,
            "test-only:" + check.check_id), revisions) for check in checks_for(kind))
    program = SecurityProgram(kind, context.plan_digest, source, target, bindings,
                              now - timedelta(seconds=1), now + timedelta(minutes=10))
    evidence = tuple(EvidenceRecord("evidence:" + binding.check_id, binding.obligation.obligation_id,
        binding.obligation.scope, revisions, binding.obligation.verifier_version,
        now - timedelta(seconds=1), now + timedelta(minutes=5), VerificationResult.PASS,
        "synthetic:" + binding.check_id, "f" * 64) for binding in bindings)
    return program, evidence


class ReviewTransport:
    def __init__(self, registrations, evidence, now):
        self.registrations, self.evidence, self.now, self.calls = registrations, evidence, now, 0

    def post(self, path, raw, credential):
        assert path == "/v1/program-reviews" and credential.token == "header.payload.signature"
        request = decode(raw)
        registration = next(value for value in self.registrations
                            if value.fingerprint == request.context_digest)
        review = review_program(registration.program, registration.phase,
                                self.evidence[registration.program.kind], now=self.now,
                                not_before=registration.not_before)
        self.calls += 1
        return encode(RemoteProgramReview(registration.fingerprint, request.request_id, "approval-host",
            "9" * 64, request.scope, request.mode, review, self.now,
            self.now + timedelta(seconds=4))).encode()


def _credential(now):
    from sova_security.adapters.review_https import BearerCredential

    return BearerCredential("header.payload.signature", now + timedelta(minutes=1))


@pytest.mark.parametrize("flow", ["post_apply"], indirect=True)
@pytest.mark.parametrize("records", ["sqlite"], indirect=True)
def test_cutover_requires_patch_postflight_and_migration_preflight(flow):  # noqa: F811
    f = flow
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        session = f.authority.load_session(connection, f.principal, plan)
    programs = SecurityProgramRecords(f.store, authorize=lambda *_: True,
        load_session=f.authority.load_session, baseline_sha=BASELINE, clock=lambda: f.state["now"])
    registrations, evidence = [], {}
    for kind, phase in ((ProgramKind.PATCH, ProgramPhase.POSTFLIGHT),
                        (ProgramKind.MIGRATION, ProgramPhase.PREFLIGHT)):
        program, evidence[kind] = _bundle(f, kind)
        registration = ReviewRegistration(program, _host_binding(program, session, plan, BASELINE), phase,
            f.state["now"], program.valid_until,
            operation_id=f.application_id if phase == ProgramPhase.POSTFLIGHT else None,
            not_before=f.state["now"] - timedelta(seconds=1) if phase == ProgramPhase.POSTFLIGHT else None)
        programs.register(f.principal, f.plan.plan_id, encode(registration), expected_revision=0,
                          command_key=f"register-{kind.value}-{phase.value}")
        registrations.append(registration)
    transport = ReviewTransport(registrations, evidence, f.state["now"])
    authority = SecurityReviewAuthority(upstream=f.authority, records=programs,
        requirements=(("patch", "postflight"), ("migration", "preflight")), transport=transport,
        credential_for=lambda: _credential(f.state["now"]), subject_id="approval-host",
        clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        result = authority(connection, f.principal, plan, now=f.state["now"].isoformat(),
                           approved_by=None, approval_id=None)
    assert transport.calls == 2
    assert result.binding["application_id"] == f.application_id
    assert len(result.revision) == 64 and result.revision != f.current.revision


@pytest.mark.parametrize("flow", ["post_apply"], indirect=True)
@pytest.mark.parametrize("records", ["sqlite"], indirect=True)
def test_revoked_registration_fails_before_remote_review(flow):  # noqa: F811
    f = flow
    programs = SecurityProgramRecords(f.store, authorize=lambda *_: True,
        load_session=f.authority.load_session, baseline_sha=BASELINE, clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        session = f.authority.load_session(connection, f.principal, plan)
    program, evidence = _bundle(f, ProgramKind.PATCH)
    registration = ReviewRegistration(program, _host_binding(program, session, plan, BASELINE),
        ProgramPhase.POSTFLIGHT, f.state["now"], program.valid_until,
        operation_id=f.application_id, not_before=f.state["now"] - timedelta(seconds=1))
    programs.register(f.principal, f.plan.plan_id, encode(registration), expected_revision=0,
                      command_key="register-patch-postflight")
    programs.revoke(f.principal, f.plan.plan_id, kind="patch", phase="postflight",
                    expected_revision=1, command_key="revoke-patch-postflight")
    with f.store._transaction(plan_id=f.plan.plan_id) as connection:
        count = connection.execute("SELECT count(*) AS n FROM plan_commands WHERE plan_id=? "
                                   "AND command_key LIKE 'security-program-patch-postflight-%'",
                                   (f.plan.plan_id,)).fetchone()["n"]
    assert count == 2
    transport = ReviewTransport([registration], {ProgramKind.PATCH: evidence}, f.state["now"])
    authority = SecurityReviewAuthority(upstream=f.authority, records=programs,
        requirements=(("patch", "postflight"),), transport=transport,
        credential_for=lambda: _credential(f.state["now"]), subject_id="approval-host",
        clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        with pytest.raises((EventError, ContractError)):
            authority(connection, f.principal, plan, now=f.state["now"].isoformat(),
                      approved_by=None, approval_id=None)
    assert transport.calls == 0


@pytest.mark.parametrize("flow", ["initial"], indirect=True)
@pytest.mark.parametrize("records", ["sqlite"], indirect=True)
def test_registration_rejects_a_different_platform_source_binding(flow):  # noqa: F811
    f = flow
    programs = SecurityProgramRecords(f.store, authorize=lambda *_: True,
        load_session=f.authority.load_session, baseline_sha=BASELINE, clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        session = f.authority.load_session(connection, f.principal, plan)
    program, _evidence = _bundle(f, ProgramKind.PATCH)
    registration = ReviewRegistration(program, _host_binding(program, session, plan, "8" * 40),
                                      ProgramPhase.PREFLIGHT, f.state["now"], program.valid_until)
    with pytest.raises(EventError, match="security_registration_binding_mismatch"):
        programs.register(f.principal, f.plan.plan_id, encode(registration), expected_revision=0,
                          command_key="wrong-platform-source")


@pytest.mark.parametrize("flow", ["initial"], indirect=True)
@pytest.mark.parametrize("records", ["sqlite"], indirect=True)
def test_change_authority_requires_patch_preflight(flow):  # noqa: F811
    f = flow
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (f.plan.plan_id,))
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        session = f.authority.load_session(connection, f.principal, plan)
    programs = SecurityProgramRecords(f.store, authorize=lambda *_: True,
        load_session=f.authority.load_session, baseline_sha=BASELINE, clock=lambda: f.state["now"])
    program, evidence = _bundle(f, ProgramKind.PATCH)
    registration = ReviewRegistration(program, _host_binding(program, session, plan, BASELINE),
                                      ProgramPhase.PREFLIGHT, f.state["now"], program.valid_until)
    programs.register(f.principal, f.plan.plan_id, encode(registration), expected_revision=0,
                      command_key="register-patch-preflight")
    current = CurrentChange({"plan_id": f.plan.plan_id}, "rehearsal", "verification", "candidate-revision")
    transport = ReviewTransport([registration], {ProgramKind.PATCH: evidence}, f.state["now"])
    checked = probe(programs, f.principal, f.plan.plan_id, "change", transport=transport,
                    credential=lambda: _credential(f.state["now"]), subject="approval-host",
                    clock=lambda: f.state["now"])
    assert checked["stage"] == "change" and checked["reviews"][0]["kind"] == "patch"
    transport.calls = 0
    authority = SecurityReviewAuthority(upstream=lambda *_args, **_kwargs: current, records=programs,
        requirements=(("patch", "preflight"),), transport=transport,
        credential_for=lambda: _credential(f.state["now"]), subject_id="approval-host",
        clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        result = authority(connection, f.principal, plan, rehearsal_id="rehearsal",
                           now=f.state["now"].isoformat(), approved_by=None, approval_id=None)
    assert transport.calls == 1
    assert result.binding == current.binding and result.revision != current.revision


@pytest.mark.parametrize("flow", ["initial"], indirect=True)
@pytest.mark.parametrize("records", ["sqlite"], indirect=True)
def test_php2shell_confirmed_compromise_blocks_the_normal_change_path(flow):  # noqa: F811
    f = flow
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (f.plan.plan_id,))
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        session = f.authority.load_session(connection, f.principal, plan)
    programs = SecurityProgramRecords(f.store, authorize=lambda *_: True,
        load_session=f.authority.load_session, baseline_sha=BASELINE, clock=lambda: f.state["now"])
    program, evidence = _bundle(f, ProgramKind.PATCH)
    evidence = tuple(replace(record, result=VerificationResult.FAIL,
        artifact_id="php2shell-confirmed-compromise:CVE-2026-109618:CVE-2026-109619")
        if record.evidence_id == "evidence:risk-assessment" else record for record in evidence)
    registration = ReviewRegistration(program, _host_binding(program, session, plan, BASELINE),
                                      ProgramPhase.PREFLIGHT, f.state["now"], program.valid_until)
    programs.register(f.principal, f.plan.plan_id, encode(registration), expected_revision=0,
                      command_key="register-php2shell-patch-preflight")
    current = CurrentChange({"plan_id": f.plan.plan_id}, "rehearsal", "verification", "candidate-revision")
    transport = ReviewTransport([registration], {ProgramKind.PATCH: evidence}, f.state["now"])
    authority = SecurityReviewAuthority(upstream=lambda *_args, **_kwargs: current, records=programs,
        requirements=(("patch", "preflight"),), transport=transport,
        credential_for=lambda: _credential(f.state["now"]), subject_id="approval-host",
        clock=lambda: f.state["now"])
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as connection:
        plan = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        with pytest.raises(ContractError, match="has not passed"):
            authority(connection, f.principal, plan, rehearsal_id="rehearsal",
                      now=f.state["now"].isoformat(), approved_by=None, approval_id=None)
    assert transport.calls == 1


def test_review_ports_rejects_config_from_another_platform_image(monkeypatch):
    monkeypatch.setenv("BRAVO_PLATFORM_SOURCE_REVISION", "1" * 40)
    config = {"url": "https://security-review.platform-state.svc:8443", "ca_file": "/missing/ca.crt",
              "token_file": "/missing/token", "subject_id": "approval-host", "source_revision": "2" * 40,
              "registrar": {"tenant_id": "tenant", "subject_id": "registrar"}}
    with pytest.raises(ValueError, match="security_review_configuration_invalid"):
        review_ports(config, clock=lambda: None)
