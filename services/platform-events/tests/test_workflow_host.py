"""The workflow host drives real WorkflowRecovery transitions from per-state readers."""

import json
import logging
import threading

import pytest
from test_cutover import SPEC
from test_workflow_recovery import ROUTE, Producers, admit_change, collect, service

from bravo_platform_events.workflow import STAGES, WorkflowRecovery
from bravo_platform_events.workflow_host import StageReaders, WorkflowHost

PLAN = SPEC.plan_id


def host(store, producers, wired=STAGES):
    readers = StageReaders({stage: producers.read for stage in wired})
    return WorkflowHost(WorkflowRecovery(service(store, None), read_evidence=readers))


def test_host_advances_a_collected_plan_until_its_producers_stop(backend, tmp_path):
    store = backend.store
    producers = Producers(tmp_path / "ledger.db")
    collect(store)
    driver = host(store, producers)
    # REHEARSE waits for T31's application: the host holds there and moves nothing.
    [step] = driver.sweep()
    assert (step.before, step.after, step.hold) == ("ANALYZE", "REHEARSE", "workflow_evidence_unavailable")
    producers.application = admit_change(store)
    [step] = driver.sweep()
    # SHIFT belongs to the cutover submit owner, so the host stops there.
    assert (step.before, step.after, step.hold) == ("REHEARSE", "SHIFT", None)
    assert store.snapshot(PLAN)["state"] == ROUTE[6] == "SHIFT"
    assert driver.active() == []


def test_unwired_state_holds_and_never_transitions(backend, tmp_path):
    store = backend.store
    collect(store)
    driver = host(store, Producers(tmp_path / "ledger.db"), wired=("ANALYZE",))
    [step] = driver.sweep()
    assert (step.after, step.hold) == ("PROPOSE", "workflow_reader_unwired")
    assert driver.sweep()[0].after == "PROPOSE"


def test_unknown_stage_reader_is_refused():
    with pytest.raises(ValueError):
        StageReaders({"SHIFT": lambda *a: None})
    with pytest.raises(ValueError):
        StageReaders({"ANALYZE": "not callable"})


def test_producer_outage_is_logged_not_raised_and_holds_log_once(backend, tmp_path, caplog):
    store = backend.store
    producers = Producers(tmp_path / "ledger.db")
    collect(store)
    driver = host(store, producers)

    def broken(c, plan, previous, run):
        raise RuntimeError("producer outage")

    driver.recovery.read_evidence = StageReaders({"ANALYZE": broken})
    caplog.set_level(logging.INFO, "bravo_platform_events.workflow_host")
    # Logged, not raised, and never a silent stall: the plan stays put with a named hold.
    assert [(s.after, s.hold) for s in driver.sweep()] == [("ANALYZE", "producer_refused:RuntimeError")]
    driver.recovery.read_evidence = StageReaders({stage: producers.read for stage in STAGES})
    driver.sweep()
    driver.sweep()
    steps = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert [(s["after"], s["hold"]) for s in steps] == [("ANALYZE", "producer_refused:RuntimeError"),
                                                        ("REHEARSE", "workflow_evidence_unavailable")]


def test_run_stops_on_event(backend, tmp_path):
    collect(backend.store)
    driver = host(backend.store, Producers(tmp_path / "ledger.db"))
    stop = threading.Event()
    stop.set()
    driver.run(stop, interval=0.01)  # one sweep, then returns
    assert backend.store.snapshot(PLAN)["state"] == "REHEARSE"


def test_a_non_event_refusal_is_a_named_hold_not_a_silent_stall(backend, monkeypatch):  # noqa: F811
    """Jeonsubb #546 P2: a T19 GateError (expired/blocked report) left GATE with no step and no hold."""
    from bravo_gate.contract import GateError

    from bravo_platform_events.workflow_host import WorkflowHost

    host = WorkflowHost.__new__(WorkflowHost)
    host.subject, host._last = "workflow-host", {}
    host.recovery = type("R", (), {"read_evidence": None,
                                   "resume_once": lambda self, p, plan: (_ for _ in ()).throw(
                                       GateError("COMBINED_GATE_EXPIRED"))})()
    step = host.advance("plan-x", "tenant-demo", "GATE", 3)
    assert (step.before, step.after, step.hold) == ("GATE", "GATE", "producer_refused:GateError:COMBINED_GATE_EXPIRED")
