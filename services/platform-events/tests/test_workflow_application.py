"""REHEARSE composition: the approved candidate recorded at PROPOSE reaches T31 unchanged."""

import json
from types import SimpleNamespace

import pytest
from bravo_apply.binding import diff_hash, render_diff
from bravo_apply.deploy import FakeKube
from test_cutover import SPEC
from test_gate_records import bundle, contract, gate_pg, ready, records, service  # noqa: F401

from bravo_platform_events import workflow_application
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow_propose import record_apply_input

TARGET = {"namespace": "target-dev", "deployment": "demo-chat", "container": "app",
          "repository": "localhost/bravo-demo-chat-candidate"}


def test_recorded_candidate_is_the_apply_input_for_the_approved_hash(backend):
    store = backend.store
    store.register_plan(SPEC, run_id="r", occurred_at="2026-11-07T00:00:00+00:00")
    edit = SimpleNamespace(model_dump=lambda mode: {"path": "app.py", "before_sha256": __import__("hashlib").sha256(b"old\n").hexdigest(),
                                                     "replacement": "new\n"})
    value = SimpleNamespace(files={"app.py": "old\n"})
    result = SimpleNamespace(proposal=SimpleNamespace(edits=[edit]))
    record = record_apply_input(store, SPEC.plan_id, value, result)
    assert record_apply_input(store, SPEC.plan_id, value, result) == record  # write-once, same bytes
    expected = render_diff({"app.py": "old\n"}, [edit.model_dump("json")])
    claim = {"binding": {"diff_hash": diff_hash(expected), "target_environment_id": SPEC.target_environment_id}}
    with store._transaction() as c:
        supplied = workflow_application.apply_input(c, SPEC.plan_id, claim, TARGET)
        assert supplied.diff == expected and supplied.proposal == {"edits": [edit.model_dump("json")]}
        assert (supplied.namespace, supplied.repository) == ("target-dev", "localhost/bravo-demo-chat-candidate")
        other = {"binding": claim["binding"] | {"diff_hash": "sha256:" + "0" * 64}}
        with pytest.raises(EventError, match="application_input_unavailable"):
            workflow_application.apply_input(c, SPEC.plan_id, other, TARGET)


def test_configuration_names_only_and_builds_rehearse(backend, tmp_path):
    path = tmp_path / "application.json"
    path.write_text(json.dumps({"receipts_dir": str(tmp_path), "kubeconfig": "/t31/kubeconfig", "target": TARGET}))
    ports = workflow_application.create(backend.store, str(path), kube=FakeKube("x@sha256:" + "0" * 64))
    assert set(ports) == {"REHEARSE", "GATE"}
    path.write_text(json.dumps({"receipts_dir": str(tmp_path), "kubeconfig": "/k", "target": TARGET | {"token": "x"}}))
    with pytest.raises(ValueError):
        workflow_application.create(backend.store, str(path), kube=FakeKube("x@sha256:" + "0" * 64))


EXPOSURE = {"environment_id": "target-dev", "namespace": "target-dev", "bucket": "demo-chat-attachments",
            "endpoint": "http://seaweedfs.target-dev.svc.cluster.local:8333", "probe_key": "attachments/t14-canary.txt",
            "base_revision": "a" * 40, "repo_root": "/platform-checkout"}


def test_an_approval_binding_a_closing_plan_hands_t31_that_plan_and_the_configured_bucket(backend):
    import bravo_iac
    from test_change_iac_binding import STATE, exposure

    from bravo_platform_events.cutover import _put
    from bravo_platform_events.workflow_propose import apply_input_key

    store = backend.store
    store.register_plan(SPEC, run_id="r", occurred_at="2026-11-07T00:00:00+00:00")
    plan = bravo_iac.plan(exposure(STATE, "open-demo"), exposure(STATE, "closed"), origin="agent")
    other = bravo_iac.plan(exposure(STATE, "open-demo"), exposure(STATE, "open-demo"), origin="agent")
    diff = b"--- a/app.py\n"
    digest = diff_hash(diff)
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        _put(c, SPEC.plan_id, apply_input_key(digest), {}, {
            "edits": [], "diff_b64": __import__("base64").b64encode(diff).decode(), "diff_hash": digest,
            "infra_plans": [other, plan]})
    binding = {"diff_hash": digest, "target_environment_id": SPEC.target_environment_id}
    with store._transaction() as c:
        assert workflow_application.apply_input(c, SPEC.plan_id, {"binding": binding}, TARGET, EXPOSURE).exposure is None
        bound = {"binding": binding | {"iac_plan_digest": plan["plan_digest"]}}
        supplied = workflow_application.apply_input(c, SPEC.plan_id, bound, TARGET, EXPOSURE)
        assert supplied.exposure["iac_plan"] == plan and supplied.exposure["environment_id"] == "target-dev"
        assert (supplied.exposure["base_revision"], supplied.exposure["repo_root"]) == ("a" * 40, "/platform-checkout")
        assert (supplied.exposure["target"].bucket, supplied.exposure["target"].namespace) == (
            "demo-chat-attachments", "target-dev")
        for claim, config in ((bound, None), ({"binding": binding | {"iac_plan_digest": "f" * 64}}, EXPOSURE)):
            with pytest.raises(EventError, match="application_exposure_unavailable"):
                workflow_application.apply_input(c, SPEC.plan_id, claim, TARGET, config)


def test_the_exposure_configuration_is_names_only(backend, tmp_path):
    path = tmp_path / "application.json"
    base = {"receipts_dir": str(tmp_path), "kubeconfig": "/t31/kubeconfig", "target": TARGET}
    path.write_text(json.dumps(base | {"exposure": EXPOSURE}))
    assert set(workflow_application.create(backend.store, str(path), kube=FakeKube("x@sha256:" + "0" * 64))) == {
        "REHEARSE", "GATE"}
    for bad in ({"access_key": "x"}, {"base_revision": "main"}, {"repo_root": "relative/checkout"}):
        path.write_text(json.dumps(base | {"exposure": EXPOSURE | bad}))
        with pytest.raises(ValueError):
            workflow_application.create(backend.store, str(path), kube=FakeKube("x@sha256:" + "0" * 64))


def test_post_apply_prepare_binds_only_a_target_run_after_the_application(records):  # noqa: F811
    from dataclasses import replace as _replace
    from datetime import timedelta
    from types import SimpleNamespace

    from bravo_gate.contract import canonical, decode
    from bravo_gate.models import RunSnapshot
    from test_gate_records import _applied_checkpoint, _recheck_case

    from bravo_platform_events.workflow_application import post_apply_prepare

    f = records
    ctx = _recheck_case(f)
    f.records.bind(f.principal, f.plan.plan_id, "initial",
                   _replace(ctx, profile_id="initial-fixture", profile_version="1"), expected_revision=0)
    _applied_checkpoint(f)
    finished = f.state["now"] + timedelta(minutes=5)
    outcome = SimpleNamespace(application_id="applied-1", finished_at=finished.isoformat())
    prepare = post_apply_prepare(f.records)
    with pytest.raises(EventError, match="post_application_target_run_required"):
        prepare(f.principal, f.plan.plan_id, outcome)  # only the pre-apply target run exists
    doc = decode(f.state["runs"]["target"].raw) | {
        "run_id": "target-after-apply", "started_at": (finished + timedelta(minutes=1)).isoformat()}
    f.records.record_run(f.principal, f.plan.plan_id, "target", RunSnapshot("target-after-apply", canonical(doc)))
    bound = prepare(f.principal, f.plan.plan_id, outcome)
    assert bound["gate_phase"] == "post_apply" and bound["context"]["target_ref"] == "target-after-apply"
    assert prepare(f.principal, f.plan.plan_id, outcome) == bound  # retry is read-only
