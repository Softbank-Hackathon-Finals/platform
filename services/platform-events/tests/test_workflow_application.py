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


def test_rehearse_refreshes_the_target_pull_secret_before_it_applies(backend, tmp_path):
    import base64

    store = backend.store
    store.register_plan(SPEC, run_id="r", occurred_at="2026-11-07T00:00:00+00:00")
    source = tmp_path / "dockerconfigjson"
    source.write_bytes(b'{"auths":{"ghcr.io":{}}}')
    path = tmp_path / "application.json"
    path.write_text(json.dumps({"receipts_dir": str(tmp_path), "kubeconfig": "/t31/kubeconfig", "target": TARGET,
                                "pull_secret": {"name": "bravo-ghcr-reader", "source": str(source)}}))
    kube = FakeKube("x@sha256:" + "0" * 64)
    kube.secrets["bravo-ghcr-reader"] = {"metadata": {"name": "bravo-ghcr-reader", "resourceVersion": "7"},
                                         "stringData": {".dockerconfigjson": '{"auths":{}}'}}
    ports = workflow_application.create(store, str(path), kube=kube)
    version = store.snapshot(SPEC.plan_id)["version"]
    for _ in range(2):
        with pytest.raises(EventError):  # no approved change yet: the apply itself still holds
            ports["REHEARSE"](SPEC.plan_id, SPEC.tenant_id, version)
    [written] = kube.secret_writes  # once: an unchanged credential is not written again
    assert written["data"] == {".dockerconfigjson": base64.b64encode(source.read_bytes()).decode()}
    assert written["metadata"]["resourceVersion"] == "7" and "stringData" not in written
    with pytest.raises(EventError):
        ports["GATE"](SPEC.plan_id, SPEC.tenant_id, version)  # GATE applies nothing: no refresh
    assert len(kube.secret_writes) == 1
