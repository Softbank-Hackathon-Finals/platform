import json
from dataclasses import dataclass

import pytest
from exec_schema import assert_valid

from rehearsal_runner.attempt_store import AttemptStore, RecordUnavailable
from rehearsal_runner.exec_contract import args_sha256, inspect_record
from rehearsal_runner.exec_flow import ExecTrace
from rehearsal_runner.exec_records import StoreJournal, collect_exec_record, merge_exec_events
from rehearsal_runner.exec_session import ExecSession, Outcome, Space
from rehearsal_runner.runner import Namespace

KEY = b"k" * 32
STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
          "ready_replicas": None, "restarts": 0, "events": None}
POD = {"target_kind": "pod", "target_name": "chat", "lines": None}


@dataclass(frozen=True)
class Admission:
    plan_id: str = "plan-1"
    tenant_id: str = "tenant-1"
    plan_version: int = 1
    task_id: str = "credential"
    rehearsal_id: str = "run-1"
    attempt: int = 1
    source_sha: str = "a" * 40
    task_digest: str = "b" * 64


class Inspect:
    def __init__(self, crash=False):
        self.crash, self.calls = crash, 0

    def validate(self, arguments):
        return inspect_record(arguments)

    def run(self, arguments, deadline):
        self.calls += 1
        if self.crash:
            raise SystemExit("runner killed while the action ran")
        return Outcome(STATUS, b"ok\n")


class Phase:
    def __init__(self, trace):
        self.traces = [trace]


def request(arguments, rid):
    return {"schema_version": 1, "run_id": "run-1", "attempt": 1, "request_id": rid,
            "args_sha256": args_sha256(arguments), "tool": "rehearsal.exec.inspect", "arguments": arguments}


def opened(tmp_path, action):
    store, admission = AttemptStore(tmp_path, KEY), Admission()
    store.claim(admission)
    session = ExecSession("run-1", 1, Space("rehearsal-a1", "uid-a"), {"inspect": action},
                          secrets=lambda: [], space_uid=lambda: "uid-a", journal=StoreJournal(store, admission))
    return store, admission, session


def trace(**values):
    return ExecTrace(space=Namespace("rehearsal-a1", "uid-a"), removed=True, opened=True, **values)


def test_each_action_leaves_signed_before_and_after_records(tmp_path):
    store, admission, session = opened(tmp_path, Inspect())
    session.handle(request(POD, "req-0001-rec"))
    session.handle(request(POD | {"namespace": "x"}, "req-0002-rec"))  # refused at VALIDATE
    records = store.read_exec(admission)
    assert set(records) == {(1, "before"), (1, "after"), (2, "before"), (2, "after")}
    assert "result_code" not in records[(1, "before")] and records[(1, "before")]["target_kind"] == "pod"
    assert merge_exec_events(records, admission) == session.events()
    body = collect_exec_record(store, admission, Phase(trace()))
    assert body["exec_events_status"] == "COMPLETE" and len(body["exec_events"]) == 2
    assert body["exec_space"] == {"namespace": "rehearsal-a1", "created_uid": "uid-a", "absence_confirmed": True}
    assert_valid("exec_record", {k: body[k] for k in ("exec_events_status", "exec_events")})


def test_crash_after_before_record_recovers_as_effect_unknown_without_rerun(tmp_path):
    store, admission, session = opened(tmp_path, Inspect(crash=True))
    with pytest.raises(SystemExit):
        session.handle(request(POD, "req-0001-crash"))
    # A restarted runner has no session; it reads only the signed journal.
    [event] = merge_exec_events(store.read_exec(admission), admission)
    assert (event["result_code"], event["reason"], event["stage"]) == ("UNKNOWN", "EFFECT_UNKNOWN", "EXECUTE")
    assert event["target_name"] == "chat"
    assert_valid("exec_event", event)


def test_exec_records_are_write_once_and_need_a_claim(tmp_path):
    store, admission, _ = opened(tmp_path, Inspect())
    store.write_exec(admission, 1, "before", {"seq": 1})
    with pytest.raises(RecordUnavailable):
        store.write_exec(admission, 1, "before", {"seq": 1})
    with pytest.raises(RecordUnavailable):
        store.write_exec(Admission(attempt=2), 1, "before", {"seq": 1})
    with pytest.raises(ValueError):
        store.write_exec(admission, 33, "before", {})


def test_journal_failure_runs_nothing(tmp_path):
    action = Inspect()

    class Broken:
        def write(self, seq, phase, body):
            raise OSError("disk full")

    session = ExecSession("run-1", 1, Space("rehearsal-a1", "uid-a"), {"inspect": action}, secrets=lambda: [],
                          space_uid=lambda: "uid-a", journal=Broken())
    response = session.handle(request(POD, "req-0001-disk"))
    assert (response["result_code"], response["reason"]) == ("REJECTED", "UNAVAILABLE")
    assert action.calls == 0 and session.journal_failed
    assert collect_exec_record(AttemptStore(tmp_path, KEY), Admission(),
                               Phase(trace(journal_failed=True)))["exec_events_status"] == "UNAVAILABLE"


def test_altered_or_foreign_exec_record_makes_the_list_unavailable(tmp_path):
    store, admission, session = opened(tmp_path, Inspect())
    session.handle(request(POD, "req-0001-alt"))
    path = tmp_path / "run-1" / "1" / "exec" / "01.after.json"
    outer = json.loads(path.read_text())
    outer["record"]["body"]["result_code"] = "FAILED"
    path.write_text(json.dumps(outer))
    assert collect_exec_record(store, admission, Phase(trace()))["exec_events_status"] == "UNAVAILABLE"


@pytest.mark.parametrize("change", [
    {(1, "after"): {"result_code": "OK", "reason": None}},
    {(1, "after"): {"result_code": "OK", "stray": "x"}},
    {(2, "before"): {"seq": 2, "run_id": "run-1", "attempt": 1, "action": "inspect", "request_id": "r" * 8,
                     "args_sha256": "0" * 64}, (3, "after"): {"result_code": "OK"}},
    {(1, "after"): {"result_code": "FAILED"}},
])
def test_merge_rejects_values_outside_the_contract(change):
    before = {"run_id": "run-1", "attempt": 1, "seq": 1, "action": "inspect", "request_id": "req-0001-x",
              "args_sha256": "0" * 64, "target_kind": "pod", "target_name": "chat"}
    records = {(1, "before"): before, (1, "after"): {"result_code": "OK"}} | change
    with pytest.raises(ValueError):
        merge_exec_events(records, Admission())
