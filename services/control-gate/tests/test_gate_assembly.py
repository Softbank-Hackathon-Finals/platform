"""Production GATE assembly (gate_services · gate_readiness) end to end on a real T10 SQLite store.

Real OperatorAccess grants, real key enrollment (PlanAuthorityKeys), real Ed25519 envelopes published
through PlanAuthorityRecords, and T13 readiness records. Only the T14 runs and the T13 document come
from the shared synthetic fixtures. The issuer key stays with the test (the operator); the store
only ever sees public keys and signed envelopes.
"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from bravo_platform_events import gate_readiness, gate_services
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.store import EventError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import GateError, canonical, fingerprint
from bravo_gate.models import RunSnapshot
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey

PURPOSES = ("context", "run-source", "run-target", "readiness-binding", "readiness-snapshot", "gate-result")
OPERATOR = ["read", "gate:record", "gate:bind", "gate:read", "gate:evaluate", "gate:consume"]


@pytest.fixture
def gate(tmp_path, ready):  # noqa: F811 - imported shared fixture
    _, state, data, doc, update = ready
    ctx, now = state["context"], state["now"]
    store = PlanStore(tmp_path / "gate.db")
    plan = PlanSpec("gate-assembly", ctx.tenant_id, ctx.app, ctx.source_environment_id,
                    ctx.target_environment_id, ctx.plan_digest, "b" * 64,
                    profile_id=ctx.profile_id, profile_version=ctx.profile_version)
    store.register_plan(plan, run_id="synthetic", occurred_at=now.isoformat())
    clock = SimpleNamespace(at=now - timedelta(hours=3))  # keys are enrolled before any collection
    access = OperatorAccess(store, clock=lambda: clock.at.isoformat())

    def who(subject):
        start = int((now - timedelta(hours=4)).timestamp())
        return OperatorPrincipal(subject, ctx.tenant_id, start, start + 6 * 3600, start, "session-" + subject)

    f = SimpleNamespace(state=state, data=data, doc=doc, update=update, store=store, plan=plan, clock=clock,
                        access=access, operator=who("gate-operator"), admin=who("gate-admin"),
                        issuer=who("gate-issuer"), keys={})
    for principal, permissions in ((f.operator, OPERATOR), (f.admin, ["read", "gate:manage-provenance"]),
                                   (f.issuer, ["read", "gate:read", "gate:publish-provenance"])):
        access.set_permissions(plan.plan_id, principal, permissions=permissions,
                               valid_after=(now - timedelta(hours=4)).isoformat(),
                               expires_at=(now + timedelta(hours=1)).isoformat(), expected_revision=0,
                               administration_ref="test-admin", command_key="grant-" + principal.subject_id)
    f.records = gate_services.gate_records(store, access)
    f.provenance = gate_services.provenance(f.records, clock=lambda: clock.at)
    f.factory = gate_services.service_factory(f.records, access, clock=lambda: clock.at)

    def enroll(purposes=PURPOSES):
        for purpose in purposes:
            key = Ed25519PrivateKey.generate()
            f.keys[purpose] = key
            f.provenance.enroll(f.admin, plan.plan_id, issuer_id="operator-" + purpose, key_id="key-one",
                                producer_subject_id=f.issuer.subject_id, expected_revision=0,
                                key=AuthorityKey(key.public_key().public_bytes_raw().hex(), (ctx.tenant_id,),
                                                 (purpose,), (clock.at - timedelta(seconds=1)).isoformat(),
                                                 (now + timedelta(hours=1)).isoformat(), False))

    def sign(purpose, subject):
        value = AuthorityEnvelope("operator-" + purpose, "key-one", purpose, ctx.fingerprint, subject,
                                  clock.at.isoformat(), (clock.at + timedelta(minutes=5)).isoformat(), "0" * 128)
        signed = replace(value, signature_hex=f.keys[purpose].sign(DOMAIN + canonical(value.payload)).hex())
        return f.provenance.publish(f.issuer, plan.plan_id, signed)

    def collect(skip=()):
        clock.at = now
        for side in ("source", "target"):
            f.records.record_run(f.operator, plan.plan_id, side, state["runs"][side])
        f.records.bind(f.operator, plan.plan_id, "context", ctx, expected_revision=0)
        gate_readiness.record_readiness(f.records, f.operator, plan.plan_id, data["binding"], data["snapshot"])
        subjects = {"context": ctx.fingerprint, "run-source": state["runs"]["source"].fingerprint,
                    "run-target": state["runs"]["target"].fingerprint,
                    "readiness-binding": data["binding"].fingerprint,
                    "readiness-snapshot": data["snapshot"].fingerprint}
        for purpose, subject in subjects.items():
            if purpose not in skip:
                sign(purpose, subject)

    f.enroll, f.sign, f.collect = enroll, sign, collect
    f.evaluate = lambda principal=None: f.records.evaluate(principal or f.operator, plan.plan_id,
                                                           service_factory=f.factory)
    return f


def test_operator_issuer_assembly_passes_and_the_signed_report_is_consumable(gate):
    f = gate
    f.enroll()
    f.collect()
    report = f.evaluate()
    assert report["decision"] == "PASS"
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.operator, plan)
        security = view.report_provider(f.operator.subject_id, view.context)
    f.sign("gate-result", security.fingerprint)
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.operator, plan)
        proof = f.factory(view).require_prerequisites(f.operator.subject_id, view.context_id, canonical(report))
    assert proof.context_digest == f.state["context"].fingerprint and not proof.authorizes_cutover


@pytest.mark.parametrize("purpose", ["context", "run-target", "readiness-binding", "readiness-snapshot"])
def test_a_missing_signature_never_passes(gate, purpose):
    f = gate
    f.enroll()
    f.collect(skip=(purpose,))
    with pytest.raises((GateError, EventError)):
        f.evaluate()


def test_evaluation_needs_the_operator_gate_grant(gate):
    f = gate
    f.enroll()
    f.collect()
    with pytest.raises((GateError, EventError)):
        f.evaluate(f.issuer)  # holds publish rights, not gate:evaluate


def test_a_key_enrolled_after_the_collection_cannot_vouch_for_it(gate):
    f = gate
    f.clock.at = f.state["now"]  # enrolled after the T14 runs started
    f.enroll()
    with pytest.raises(GateError):
        f.collect()


def test_readiness_is_bound_to_the_current_context_and_the_newest_is_served(gate):
    f = gate
    f.enroll()
    f.collect()
    # A capture made for another context, consistent in itself, is refused by the current binding.
    foreign = dict(f.doc, context_digest="c" * 64)
    snapshot = RunSnapshot("readiness-ref", canonical(foreign))
    other = replace(f.data["binding"], context_digest="c" * 64, snapshot_digest=fingerprint(foreign))
    with pytest.raises(EventError, match="gate_readiness_context_mismatch"):
        gate_readiness.record_readiness(f.records, f.operator, f.plan.plan_id, other, snapshot)
    first = f.data["binding"]
    later = dict(f.doc, replication=dict(f.doc["replication"],
                                         observed_at=(f.state["now"] + timedelta(seconds=10)).isoformat()))
    f.update(later)
    gate_readiness.record_readiness(f.records, f.operator, f.plan.plan_id, f.data["binding"], f.data["snapshot"])
    with f.store._transaction(plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.operator, plan)
        served = gate_readiness.binding_provider(view, f.operator.subject_id, view.context)
        assert served == f.data["binding"] != first
        assert gate_readiness.snapshot_provider(view, f.operator.subject_id, view.context, first) is None


def test_one_readiness_component_observed_before_enrollment_cannot_be_vouched_for(gate):
    f = gate
    f.enroll()
    f.collect()
    early = (f.clock.at - timedelta(hours=4)).isoformat()  # before the keys were enrolled
    f.update(dict(f.doc, replication=dict(f.doc["replication"], observed_at=early)))
    gate_readiness.record_readiness(f.records, f.operator, f.plan.plan_id, f.data["binding"], f.data["snapshot"])
    with pytest.raises(GateError):
        f.sign("readiness-snapshot", f.data["snapshot"].fingerprint)


def test_consuming_the_report_needs_its_own_gate_consume_grant(gate):
    f = gate
    f.enroll()
    f.collect()
    report = f.evaluate()
    viewer = replace(f.operator, subject_id="gate-viewer", session_id="session-gate-viewer")
    f.access.set_permissions(f.plan.plan_id, viewer, permissions=["read", "gate:read", "gate:evaluate"],
                             valid_after=(f.state["now"] - timedelta(hours=4)).isoformat(),
                             expires_at=(f.state["now"] + timedelta(hours=1)).isoformat(), expected_revision=0,
                             administration_ref="test-admin", command_key="grant-gate-viewer")
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, f.operator, plan)
        security = view.report_provider(f.operator.subject_id, view.context)
    f.sign("gate-result", security.fingerprint)
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        view = f.records.view(c, viewer, plan)  # the viewer may read; only gate:consume is missing
        with pytest.raises(GateError):
            f.factory(view).require_prerequisites(viewer.subject_id, view.context_id, canonical(report))


def test_load_gate_serves_the_same_assembly_and_refuses_without_readiness(gate):
    f = gate
    f.enroll()
    load = gate_services.load_gate(f.records, f.access, clock=lambda: f.clock.at)
    f.clock.at = f.state["now"]
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    with f.store._transaction(plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        with pytest.raises(EventError, match="gate_readiness_unavailable"):
            load(f.records.view(c, f.operator, plan))
    gate_readiness.record_readiness(f.records, f.operator, f.plan.plan_id, f.data["binding"], f.data["snapshot"])
    with f.store._transaction(plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        service, readiness = load(f.records.view(c, f.operator, plan))
        assert readiness == f.data["binding"] and service.binding_provider is not None


def test_create_is_the_operator_host_gate_factory(gate):
    from bravo_platform_events.gate_records import GateRecords

    records, load = gate_services.create(gate.store)
    assert type(records) is GateRecords and callable(load)
