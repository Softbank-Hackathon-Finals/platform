"""The workflow host moves PROPOSE -> REHEARSE from the real T10 journal and a pinned T7 run."""

import json
from dataclasses import asdict

import pytest
import test_initial_proposal as original
from test_candidate_publication import Bundle, repository  # noqa: F401  (fixture)
from test_cutover import SPEC
from test_workflow_recovery import service

from bravo_platform_events import candidate_publication as candidate
from bravo_platform_events.workflow import WorkflowRecovery
from bravo_platform_events.workflow_host import StageReaders, WorkflowHost
from bravo_platform_events.workflow_propose import pinned_selections, propose_preparer

PLAN = SPEC.plan_id


def setup(backend, repository, tmp_path, monkeypatch, *, execute=True, writer=None):  # noqa: F811
    b = Bundle(repository)
    monkeypatch.setattr(original, "SOURCE", repository[1])
    f = original.Fixture(backend, tmp_path)
    if execute:
        f.execute()
        b.value, b.intent, b.result = f.port.publication_inputs(PLAN, 3, **f.ref)
        b.receipt["request_id"] = candidate.publication_request_id(b.intent)
        b.reseal()
    pins = tmp_path / "selections"
    pins.mkdir()
    prepare = propose_preparer(f.port, select=pinned_selections(pins), repository=b.root, github=b.provider,
                               writer=writer(b) if writer else None)
    readers = StageReaders({}, preparers={"PROPOSE": prepare})
    host = WorkflowHost(WorkflowRecovery(service(backend.store, None), read_evidence=readers))
    return b, f, pins, host


def pin(b, pins):
    (pins / f"{candidate.publication_request_id(b.intent)}.json").write_text(json.dumps(asdict(b.selected)))


def test_pinned_t7_run_moves_propose_to_rehearse(backend, repository, tmp_path, monkeypatch):  # noqa: F811
    b, f, pins, host = setup(backend, repository, tmp_path, monkeypatch)
    [step] = host.sweep()
    assert (step.before, step.after, step.hold) == ("PROPOSE", "PROPOSE", "initial_publication_unselected")
    pin(b, pins)
    [step] = host.sweep()
    # REHEARSE is not wired in this host, so it stops there.
    assert (step.before, step.after, step.hold) == ("PROPOSE", "REHEARSE", "workflow_reader_unwired")
    assert f.port.stored(PLAN, 3, **f.ref)["proposal_digest"] == b.result.proposal_digest
    assert len(f.client.submits) == 1  # the host never submits to the model


def test_unconfirmed_publication_and_missing_execution_hold(backend, repository, tmp_path, monkeypatch):  # noqa: F811
    b, f, pins, host = setup(backend, repository, tmp_path, monkeypatch)
    b.reseal({"digest": "not-a-digest"})
    pin(b, pins)
    assert host.sweep()[0].hold == "initial_publication_unconfirmed"
    assert backend.store.snapshot(PLAN)["state"] == "PROPOSE"


def test_no_model_result_yet_holds(backend, repository, tmp_path, monkeypatch):  # noqa: F811
    _b, _f, _pins, host = setup(backend, repository, tmp_path, monkeypatch, execute=False)
    [step] = host.sweep()
    assert step.after == "PROPOSE" and step.hold is not None


def test_selection_mount_must_be_absolute_dir(tmp_path):
    with pytest.raises(ValueError):
        pinned_selections("relative")
    with pytest.raises(ValueError):
        propose_preparer(object(), select=lambda _id: None, repository=tmp_path)


def test_without_a_pin_the_app_writer_supplies_the_selection(backend, repository, tmp_path, monkeypatch):  # noqa: F811
    asked = []

    class Writer:
        def __init__(self, b):
            self.b = b

        def selection(self, value, result, request_id):
            asked.append(request_id)
            return self.b.selected

    b, f, _pins, host = setup(backend, repository, tmp_path, monkeypatch, writer=Writer)
    [step] = host.sweep()
    assert step.after == "REHEARSE" and asked == [candidate.publication_request_id(b.intent)]


def test_pins_resolve_from_a_folder_tree_or_a_flat_configmap_mount(tmp_path):
    """A ConfigMap mount cannot hold sub-folders: <dir>__<id>.json in the parent is the same pin;
    an unmounted folder means no pins; an id never leaves the mount."""
    selection = {"run_id": 1, "head_sha": "a" * 40}
    flat = tmp_path / "workflow"
    flat.mkdir()
    (flat / "selections__t10p-abc.json").write_text(json.dumps(selection))
    seen = []
    import bravo_platform_events.workflow_propose as wp
    original = wp.PublicationSelection
    wp.PublicationSelection = lambda **kw: seen.append(kw) or kw
    try:
        assert pinned_selections(flat / "selections")("t10p-abc") == selection
        (flat / "selections").mkdir()
        (flat / "selections" / "t10p-abc.json").write_text(json.dumps(selection | {"run_id": 2}))
        assert pinned_selections(flat / "selections")("t10p-abc")["run_id"] == 2  # the real tree wins
        assert pinned_selections(tmp_path / "not-mounted")("t10p-abc") is None
        for bad in ("../workflow/selections__t10p-abc", "a/b", "", None):
            assert pinned_selections(flat / "selections")(bad) is None
    finally:
        wp.PublicationSelection = original
