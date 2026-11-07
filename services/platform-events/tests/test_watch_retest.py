"""WATCH control retest over real T10 gate records and T19's evaluator; runs are the shared examples."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from bravo_gate.contract import canonical
from bravo_gate.models import RunSnapshot
from test_gate_records import bundle, contract, gate_pg, ready, records, service  # noqa: F401

from bravo_platform_events import watch_retest
from bravo_platform_events.cutover import _key
from bravo_platform_events.workflow import Run

RUN = Run("cutover-command", 7, "execution-1")


def shifted(doc, delta, run_id, **changes):
    """The same target run taken `delta` later (a fresh T14 collection)."""
    doc = deepcopy(doc)
    doc["run_id"] = run_id
    for key in ("started_at", "finished_at"):
        doc[key] = (watch_retest.time(doc[key]) + delta).isoformat()
    for row in doc["results"]:
        if row["evidence"] is not None:
            for key in ("collected_at", "expires_at"):
                row["evidence"][key] = (watch_retest.time(row["evidence"][key]) + delta).isoformat()
        row.update(changes.get(row["test_id"], {}))
    return doc


@pytest.fixture
def watch(records):  # noqa: F811 - imported shared fixture
    f = records
    f.stage("source")
    f.stage("target")
    f.bind()
    f.cutover = f.state["now"] + timedelta(minutes=20)  # the cutover's last observed update
    f.now = f.cutover + timedelta(minutes=30)
    f.target = json.loads(f.state["runs"]["target"].raw)

    def record(doc):
        f.records.record_run(f.principal, f.plan.plan_id, "target", RunSnapshot(doc["run_id"], canonical(doc)))

    def retest(since=lambda *_: f.cutover):
        reader = watch_retest.retest_reader(watch_retest.T5Contract(), clock=lambda: f.now, since=since)
        with f.store._transaction(plan_id=f.plan.plan_id) as c:
            plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone())
            return reader(c, plan, RUN)

    f.record, f.retest = record, retest
    return f


def test_a_target_run_after_the_cutover_that_holds_the_controls_passes(watch):
    f = watch
    assert f.retest() == "UNKNOWN"  # only the GATE-time target run: nothing after the cutover yet
    f.record(shifted(f.target, timedelta(minutes=25), "run-after-cutover"))
    assert f.retest() == "PASS"


def test_a_control_that_regressed_after_the_cutover_fails(watch):
    f = watch
    f.record(shifted(f.target, timedelta(minutes=25), "run-regressed",
                     **{"IAM-01.1": {"verdict": "FAIL", "observed": "allow"}}))
    assert f.retest() == "FAIL"


def test_the_newest_run_after_the_cutover_decides(watch):
    f = watch
    regressed = {"IAM-01.1": {"verdict": "FAIL", "observed": "allow"}}
    f.record(shifted(f.target, timedelta(minutes=25), "run-fixed"))
    f.record(shifted(f.target, timedelta(minutes=22), "run-first", **regressed))  # older, recorded later
    assert f.retest() == "PASS"
    f.record(shifted(f.target, timedelta(minutes=28), "run-latest", **regressed))
    assert f.retest() == "FAIL"
    f.record(shifted(f.target, timedelta(minutes=29), "run-fixed-again"))
    assert f.retest() == "PASS"
    # A run taken before the cutover never counts, however late it was recorded.
    f.record(shifted(f.target, timedelta(minutes=10), "run-before-cutover",
                     **{"IAM-01.1": {"verdict": "FAIL", "observed": "allow"}}))
    assert f.retest() == "PASS"


def test_what_cannot_be_judged_waits_and_is_never_a_failure(watch):
    f = watch
    f.record(shifted(f.target, timedelta(minutes=25), "run-other-method",
                     **{"SEC-01.1": {"method_version": "2"}}))  # T19: METHOD_MISMATCH
    assert f.retest() == "UNKNOWN"
    f.record(shifted(f.target, timedelta(minutes=27), "run-ok"))
    f.now += timedelta(days=2)  # the GATE source run's evidence has expired
    assert f.retest() == "UNKNOWN"


def test_without_a_cutover_observation_the_retest_waits(watch):
    f = watch
    f.record(shifted(f.target, timedelta(minutes=25), "run-after-cutover"))
    assert f.retest(since=watch_retest.cutover_finished) == "UNKNOWN"  # no observation stored
    with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (f.plan.plan_id, _key("observation", RUN.command_id) + "-000001", "{}",
                   json.dumps({"index": 1, "outcome": "OBSERVED",
                               "snapshot": {"updated_at": f.cutover.isoformat()}})))
    assert f.retest(since=watch_retest.cutover_finished) == "PASS"


def test_create_is_the_ports_json_factory(watch):
    assert callable(watch_retest.create(watch.store))
