"""Host recheck uses real T19 evaluation with synthetic producer records."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from bravo_apply.path import GATE_RECHECK_TESTS, ApplyOutcome, _create_record
from bravo_gate.contract import canonical
from bravo_gate.models import RunSnapshot
from bravo_gate.network import VerifiedNetworkProof
from test_gate_records import (  # noqa: F401
    _applied_checkpoint,
    bundle,
    contract,
    gate_pg,
    ready,
    records,
    service,
)

from bravo_platform_events.application_host import ApplicationEvidence, GateRecheck
from bravo_platform_events.cutover import _put
from bravo_platform_events.plans import change_key
from bravo_platform_events.postgres import PostgresPlanStore, _Queries
from bravo_platform_events.store import EventError


def outcome(plan_id):
    digest = "sha256:" + "a" * 64
    return ApplyOutcome(schema_version="1.1", application_id="application-fixture",
        approval_id="approval-fixture", verification_id="verification-fixture", plan_id=plan_id,
        outcome="APPLIED", reason="APPLIED_AS_APPROVED",
        change=dict(diff_hash="sha256:" + "b" * 64, image_digest=digest),
        deployed_digest=digest, job_passed=True,
        gate_recheck=dict(plan_id=plan_id, application_id="application-fixture",
                          image_digest=digest, tests=list(GATE_RECHECK_TESTS)),
        cluster="kubectl", target=dict(namespace="fixture", deployment="fixture",
            container="app", repository="ghcr.io/example/demo-chat"), cluster_mutation_attempted=True,
        previous_image="fixture", observed_digests=[digest],
        started_at="2026-11-04T11:28:00Z", deadline_at=None, finished_at="2026-11-04T11:29:00Z")


@contextmanager
def fixture_admin(f, pg):
    if isinstance(f.store, PostgresPlanStore):
        name = psycopg.conninfo.conninfo_to_dict(f.store._conninfo)["dbname"]
        with pg.connect(name) as c:
            c.row_factory = psycopg.rows.dict_row
            c.execute("SET search_path = pg_catalog, bravo_platform")
            yield _Queries(c)
    else:
        with f.store._connection() as c:
            yield c


def stage(f, pg, *, profile=None, complete=False):
    if profile:
        doc = f.state["runs"]["target"].document
        doc["results"].append(dict(test_id="ENC-01.1", control_id="ENC-01", method_version="1",
            expected="success", observed="success", positive_control="not_applicable", verdict="PASS",
            unknown_reason=None, evidence=dict(doc["results"][0]["evidence"])))
        target = RunSnapshot("target-full", canonical(doc))
        f.state["runs"]["target"] = target
        f.state["context"] = replace(f.state["context"], profile_id=profile,
            target_ref=target.ref, target_digest=target.fingerprint)
        if complete:
            for side in ("source", "target"):
                doc = f.state["runs"][side].document
                evidence = doc["results"][0]["evidence"]
                catalog = f.records.contract.catalog
                ids = f.records.contract.profile(profile, "1")["required_test_ids"]
                doc["results"] = [catalog["tests"][test] | dict(test_id=test,
                    observed=catalog["tests"][test]["expected"], verdict="PASS",
                    positive_control=("pass" if catalog["tests"][test]["positive_control"] == "required"
                                      else "not_applicable"),
                    unknown_reason=None, evidence=dict(evidence)) for test in ids]
                snapshot = RunSnapshot(side + "-complete", canonical(doc))
                f.state["runs"][side] = snapshot
                f.state["context"] = replace(f.state["context"], **{
                    side + "_ref": snapshot.ref, side + "_digest": snapshot.fingerprint})
        f.update(f.doc)
    # Only the test admin can seed an immutable registered profile.
    with fixture_admin(f, pg) as c:
        row = c.execute("SELECT spec_json FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone()
        spec = json.loads(row["spec_json"])
        spec.update(recheck_profile_id=profile or spec["profile_id"],
                    recheck_profile_version="1")
        c.execute("UPDATE plans SET spec_json=? WHERE plan_id=?",
                  (json.dumps(spec), f.plan.plan_id))
    _applied_checkpoint(f, application_id="application-fixture")
    f.stage("source"), f.stage("target")
    f.records.bind(f.principal, f.plan.plan_id, "context", f.state["context"],
                   expected_revision=0, gate_phase="post_apply", application_id="application-fixture")


def test_demo_profile_missing_requested_encryption_cannot_pass(records, gate_pg):  # noqa: F811
    f = records
    stage(f, gate_pg, profile="demo-gate")
    calls = []
    gate = GateRecheck(f.records, prepare=lambda *a: calls.append(a), service_factory=f.factory)
    with pytest.raises(EventError, match="gate_recheck_profile_incomplete"):
        gate.evaluate(f.principal, f.plan.plan_id, outcome(f.plan.plan_id))
    assert len(calls) == 1
    assert f.store.snapshot(f.plan.plan_id)["state"] == "GATE"


def test_real_t19_blocks_incomplete_full_profile_even_with_three_controls(records, gate_pg):  # noqa: F811
    f = records
    stage(f, gate_pg, profile="full")
    gate = GateRecheck(f.records, prepare=lambda *a: None, service_factory=f.factory)
    report = gate.evaluate(f.principal, f.plan.plan_id, outcome(f.plan.plan_id))
    assert report["decision"] == "BLOCK"
    assert f.store.snapshot(f.plan.plan_id)["state"] == "GATE"


@pytest.mark.parametrize("profile", ["full", "apply-recheck"])
def test_authenticated_post_apply_report_is_consumed_and_revocation_blocks(records, gate_pg, tmp_path, profile):  # noqa: F811
    f = records
    stage(f, gate_pg, profile=profile, complete=True)
    def factory(view):
        selected = f.factory(view)
        # Synthetic network authority; no runtime network success is claimed.
        selected.security.network_verifier = lambda actor, ctx, side, test, row: VerifiedNetworkProof(
            ctx.fingerprint, side, test, row["evidence"]["output_sha256"],
            f.state["now"] + timedelta(seconds=30))
        return selected
    result = outcome(f.plan.plan_id)
    assert GateRecheck(f.records, prepare=lambda *a: None, service_factory=factory).evaluate(
        f.principal, f.plan.plan_id, result)["decision"] == "PASS"
    _create_record(tmp_path, result)
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        _put(c, f.plan.plan_id, change_key("consumed", result.approval_id), {}, dict(
            application_id=result.application_id, verification_id=result.verification_id,
            binding=dict(plan_digest=f.plan.plan_digest, target_environment_id=f.plan.target_environment_id,
                         image_digest=result.deployed_digest, diff_hash=result.change["diff_hash"])))
    reader = ApplicationEvidence(principal=f.principal, application_id=result.application_id,
        receipts_dir=tmp_path, records=f.records, check_current=lambda *a: True,
        load_gate=lambda view: (factory(view), f.data["binding"]), fallback=lambda *a: None)
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
        previous = dict(result=dict(application_id=result.application_id))
        assert reader(c, plan, previous, None).result.verdict == "PASS"
        f.allowed = False
        with pytest.raises(EventError, match="forbidden"):
            reader(c, plan, previous, None)


@pytest.mark.parametrize("kind", ["REFUSED", "SAFE_HOLD", "UNKNOWN", "fake", "partial"])
def test_failed_or_fake_apply_never_triggers_recheck(records, kind):  # noqa: F811
    f = records
    result = outcome(f.plan.plan_id)
    if kind == "fake":
        result.cluster = "fake"
    elif kind == "partial":
        result.gate_recheck["tests"] = ["IAM-01.1"]
    else:
        result.outcome = kind
    gate = GateRecheck(f.records, prepare=lambda *a: pytest.fail("recheck was dispatched"),
                      service_factory=f.factory)
    with pytest.raises(EventError):
        gate.evaluate(f.principal, f.plan.plan_id, result)
