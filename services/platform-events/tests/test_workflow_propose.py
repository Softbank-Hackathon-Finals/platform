"""The workflow host moves PROPOSE -> REHEARSE from the real T10 journal and a platform-built candidate."""

import pytest
import test_initial_proposal as original
from test_candidate_build import FakeRegistry, seed
from test_cutover import SPEC
from test_workflow_recovery import service

from bravo_platform_events.candidate_build import PlatformCandidateBuilder
from bravo_platform_events.workflow import WorkflowRecovery
from bravo_platform_events.workflow_host import StageReaders, WorkflowHost
from bravo_platform_events.workflow_propose import propose_preparer

PLAN = SPEC.plan_id


def setup(backend, tmp_path, *, execute=True, copied=("app/",), source_sha=original.SOURCE):
    f = original.Fixture(backend, tmp_path)
    if execute:
        f.execute()
    registry = FakeRegistry()
    build = PlatformCandidateBuilder(base_file=seed(registry, tmp_path, source_sha=source_sha, copied=copied),
                                     credentials_file=tmp_path / "unused", receipts_dir=tmp_path / "receipts",
                                     registry=registry)
    readers = StageReaders({}, preparers={"PROPOSE": propose_preparer(f.port, build=build)})
    host = WorkflowHost(WorkflowRecovery(service(backend.store, None), read_evidence=readers))
    return f, registry, host


def test_platform_build_moves_propose_to_rehearse(backend, tmp_path):
    f, registry, host = setup(backend, tmp_path)
    [step] = host.sweep()
    # REHEARSE is not wired in this host, so it stops there.
    assert (step.before, step.after, step.hold) == ("PROPOSE", "REHEARSE", "workflow_reader_unwired")
    assert f.port.stored(PLAN, 3, **f.ref)["proposal_digest"]
    assert any(kind == "manifest" and ref.startswith("platform-candidate-") for kind, ref in registry.writes)
    assert len(f.client.submits) == 1  # the host never submits to the model


@pytest.mark.parametrize("seeded, hold", [
    ({"copied": ("lib/",)}, "candidate_rebuild_required"),
    ({"source_sha": "c" * 40}, "candidate_base_mismatch"),
])
def test_unbuildable_candidates_hold_in_propose(backend, tmp_path, seeded, hold):
    _f, registry, host = setup(backend, tmp_path, **seeded)
    assert host.sweep()[0].hold == hold
    assert backend.store.snapshot(PLAN)["state"] == "PROPOSE" and registry.writes == []


def test_a_registry_outage_holds_and_the_next_sweep_finishes(backend, tmp_path):
    _f, registry, host = setup(backend, tmp_path)
    registry.fail = "blob"
    assert host.sweep()[0].hold == "candidate_registry_write_failed"
    registry.fail = None
    assert host.sweep()[0].after == "REHEARSE"


def test_no_model_result_yet_holds(backend, tmp_path):
    _f, _registry, host = setup(backend, tmp_path, execute=False)
    [step] = host.sweep()
    assert step.after == "PROPOSE" and step.hold is not None


def test_ports_are_required(tmp_path):
    with pytest.raises(ValueError):
        propose_preparer(object(), build=object())
