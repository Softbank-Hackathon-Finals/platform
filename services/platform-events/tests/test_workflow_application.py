"""REHEARSE composition: the approved candidate recorded at PROPOSE reaches T31 unchanged."""

import json
from types import SimpleNamespace

import pytest
from bravo_apply.binding import diff_hash, render_diff
from bravo_apply.deploy import FakeKube
from test_cutover import SPEC

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
    assert set(ports) == {"REHEARSE"}
    path.write_text(json.dumps({"receipts_dir": str(tmp_path), "kubeconfig": "/k", "target": TARGET | {"token": "x"}}))
    with pytest.raises(ValueError):
        workflow_application.create(backend.store, str(path), kube=FakeKube("x@sha256:" + "0" * 64))
