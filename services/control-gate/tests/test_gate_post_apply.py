"""After the T31 application: the post-apply GATE is signed again by the operator issuer and consumed
from a platform re-evaluation (workflow GATE reader, #565), never from an operator gate-result signature.

Real T10 store and OperatorAccess grants, post_apply_prepare, the operator relay and issuer CLI (#573),
served_reevaluation. The T14 runs and T13 document are the shared synthetic fixtures.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from bravo_platform_events import gate_relay, gate_services
from bravo_platform_events.gate_records import served_reevaluation
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow_application import post_apply_prepare
from test_gate_assembly import OPERATOR, gate  # noqa: F401
from test_gate_relay import cli
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import GateError, canonical, time
from bravo_gate.models import GateContext, RunSnapshot

TTL = timedelta(minutes=5)
READINESS = ["readiness-binding", "readiness-snapshot"]


def shifted(doc, delta, run_id):
    """The same T14 run taken `delta` later (a fresh collection after the application)."""
    doc = deepcopy(doc)
    doc["run_id"] = run_id
    for key in ("started_at", "finished_at"):
        doc[key] = (time(doc[key]) + delta).isoformat()
    for row in doc["results"]:
        if row["evidence"] is not None:
            for key in ("collected_at", "expires_at"):
                row["evidence"][key] = (time(row["evidence"][key]) + delta).isoformat()
    return doc


@pytest.fixture
def post(gate, tmp_path):  # noqa: F811 - imported shared fixture
    f = gate
    ctx, now = f.state["context"], f.state["now"]
    f.plan = replace(f.plan, plan_id="gate-post-apply", recheck_profile_id=ctx.profile_id,
                     recheck_profile_version=ctx.profile_version)
    f.store.register_plan(f.plan, run_id="synthetic", occurred_at=now.isoformat())
    for principal, permissions in ((f.operator, OPERATOR), (f.admin, ["read", "gate:manage-provenance"]),
                                   (f.issuer, ["read", "gate:read", "gate:publish-provenance"])):
        f.access.set_permissions(f.plan.plan_id, principal, permissions=permissions,
                                 valid_after=(now - timedelta(hours=4)).isoformat(),
                                 expires_at=(now + timedelta(hours=1)).isoformat(), expected_revision=0,
                                 administration_ref="test-admin", command_key="grant-post-" + principal.subject_id)
    config = {"tenant_id": ctx.tenant_id, "admin": "gate-admin", "issuer": "gate-issuer",
              "operator": "gate-operator"}
    f.call = lambda request: gate_relay.handle(f.records, request, config, clock=lambda: f.clock.at)
    f.load = gate_services.load_gate(f.records, f.access, clock=lambda: f.clock.at)
    f.keys = cli.load_keys(tmp_path / "keys", create=True)
    for request in cli.enroll_requests(tmp_path / "keys", f.plan.plan_id, f.keys, cli.PURPOSES, f.clock.at, 6):
        f.call(request)

    def subjects():
        return f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]

    def sign(purposes=None):
        shown = subjects()
        return [f.call(r)["published"]["purpose"]
                for r in cli.sign_requests(f.plan.plan_id, shown, f.keys, purposes, shown["confirm"], f.clock.at, TTL)]

    def capture():
        """A T13 capture for the current context, observed now (each component expires in 30 s)."""
        doc = deepcopy(f.doc)
        at, until = f.clock.at.isoformat(), (f.clock.at + timedelta(seconds=30)).isoformat()
        for part in [doc["source"], doc["target"], doc["replication"], *doc["sequences"]]:
            part.update(observed_at=at, expires_at=until)
        f.update(doc)
        f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
                "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})

    def transaction():
        return f.store._transaction(write=True, plan_id=f.plan.plan_id)

    def view(c):
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        return f.records.view(c, f.operator, plan)

    f.subjects, f.sign, f.capture, f.transaction, f.view = subjects, sign, capture, transaction, view
    return f


def applied(f, application_id, finished):
    """The successful T31 application checkpoint (WorkflowRecovery output) and the plan at GATE."""
    proof = dict(plan_id=f.plan.plan_id, plan_digest=f.plan.plan_digest,
                 target_environment_id=f.plan.target_environment_id, version=4, receipt_id=application_id,
                 result=dict(application_id=application_id, job_passed=True, deployed_digest="sha256:" + "c" * 64,
                             change=dict(image_digest="sha256:" + "c" * 64)))
    with f.transaction() as c:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (f.plan.plan_id, "workflow:4", "{}", json.dumps(proof)))
        c.execute("UPDATE plans SET state='GATE',version=5 WHERE plan_id=?", (f.plan.plan_id,))
    return SimpleNamespace(application_id=application_id, finished_at=finished.isoformat())


def test_the_post_apply_gate_is_signed_again_and_consumed_from_a_platform_reevaluation(post):
    f = post
    now = f.state["now"]
    # Before the application: the initial context, signed by the operator issuer.
    f.clock.at = now
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    assert set(f.sign()) == {"context", "run-source", "run-target"}
    initial = f.subjects()

    # T31 applied; T14 collects the target again after it finished; GATE prepare binds that run.
    outcome = applied(f, "applied-1", now + timedelta(minutes=5))
    target = shifted(json.loads(f.state["runs"]["target"].raw), timedelta(minutes=10), "run-after-apply")
    f.clock.at = now + timedelta(minutes=15)
    f.records.record_run(f.operator, f.plan.plan_id, "target", RunSnapshot("run-after-apply", canonical(target)))
    bound = post_apply_prepare(f.records)(f.operator, f.plan.plan_id, outcome)
    assert bound["gate_phase"] == "post_apply"
    f.state["context"] = GateContext(**bound["context"])

    # A new context: nothing signed before the application counts, and T13 is captured again for it.
    shown = f.subjects()
    assert shown["context"]["digest"] != initial["context"]["digest"] and shown["published"] == []
    assert shown["run-source"] == initial["run-source"] and shown["run-target"] != initial["run-target"]
    assert "readiness-binding" not in shown
    f.capture()
    assert set(f.sign()) == {"context", "run-source", "run-target", "readiness-binding", "readiness-snapshot"}
    report = f.records.evaluate(f.operator, f.plan.plan_id, service_factory=f.factory)
    assert report["decision"] == "PASS" and report["context_digest"] == f.state["context"].fingerprint
    assert "gate-result" in f.subjects()  # offered, optional: never signed in this test

    # Seventy seconds later the stored report has expired and has no gate-result signature.
    f.clock.at += timedelta(seconds=70)
    with f.transaction() as c:
        v = f.view(c)
        service, readiness = f.load(v)
        session = v.session(service, readiness)
        with pytest.raises(GateError):
            service.require_prerequisites(f.operator.subject_id, session.context_id, session.report.raw,
                                          approval_ids=session.approval_ids)
    # A fresh T13 capture, signed, and the reader consumes the platform re-evaluation (#565 e240c224).
    f.capture()
    assert set(f.sign(READINESS)) == {"readiness-binding", "readiness-snapshot"}
    with f.transaction() as c:
        v = f.view(c)
        service, readiness = f.load(v)
        session = v.session(service, readiness)
        with served_reevaluation(c, f.plan.plan_id, session, f.operator) as fresh:
            proof = service.require_prerequisites(f.operator.subject_id, session.context_id, canonical(fresh),
                                                  approval_ids=session.approval_ids)
    assert proof.context_digest == f.state["context"].fingerprint and not proof.authorizes_cutover


def test_an_unsigned_newer_capture_blocks_the_reevaluation_until_it_is_signed(post):
    f = post
    now = f.state["now"]
    f.clock.at = now
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    f.capture()
    f.sign()
    assert f.records.evaluate(f.operator, f.plan.plan_id, service_factory=f.factory)["decision"] == "PASS"
    f.clock.at += timedelta(seconds=70)
    f.capture()  # recorded, not signed: the newest record is what the gate reads

    def consume():
        with f.transaction() as c:
            v = f.view(c)
            service, readiness = f.load(v)
            session = v.session(service, readiness)
            with served_reevaluation(c, f.plan.plan_id, session, f.operator) as fresh:
                return service.require_prerequisites(f.operator.subject_id, session.context_id,
                                                     canonical(fresh), approval_ids=session.approval_ids)

    with pytest.raises((GateError, EventError)):
        consume()
    f.sign(READINESS)
    assert consume().context_digest == f.state["context"].fingerprint
