import pytest
from exec_schema import assert_valid

from rehearsal_runner.exec_contract import args_sha256, inspect_record
from rehearsal_runner.exec_flow import ExecConfig, ExecRegistry, ExecTrace, run_with_exec
from rehearsal_runner.exec_session import ExecSession, Outcome, Space
from rehearsal_runner.isolation_gate import IsolationBlocked, IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once

RUN = "plan-1-a"
SHA = "a" * 40
PROPOSAL = ProposalRef(RUN, SHA, "b" * 64)
STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
          "ready_replicas": None, "restarts": 0, "events": None}


class Task:
    run_id, source_sha = RUN, SHA


class Lifecycle:
    def __init__(self, log, label, *, cleaned=True):
        self.log, self.label, self.cleaned, self.n = log, label, cleaned, 0

    def create(self, run_id):
        self.n += 1
        self.log.append(f"create {self.label}")
        return Namespace(f"rehearsal-{self.label}{self.n}", f"uid-{self.label}{self.n}")

    def cleanup(self, namespace):
        self.log.append(f"cleanup {namespace.name}")
        return self.cleaned


class Isolation:
    def __init__(self, log, *, blocked=False):
        self.log, self.blocked = log, blocked

    def check(self, namespace):
        self.log.append(f"isolate {namespace.name}")
        if self.blocked:
            raise IsolationBlocked()
        return IsolationPass(namespace.name, namespace.uid, "probe-1")


class Verifier:
    attempt = 1

    def __init__(self, log):
        self.log = log

    def verify_isolated(self, proposal, namespace, isolation):
        self.log.append(f"verify {namespace.name}")
        return {"namespace": namespace.name}


class Inspect:
    def validate(self, arguments):
        return inspect_record(arguments)

    def run(self, arguments, deadline):
        return Outcome(STATUS)


class Phase:
    def __init__(self, log, registry, *, open_fails=False, isolation_blocked=False, cleaned=True):
        self.log, self.registry, self.open_fails = log, registry, open_fails
        self.lifecycle = Lifecycle(log, "a", cleaned=cleaned)
        self.isolation = Isolation(log, blocked=isolation_blocked)
        self.traces: list[ExecTrace] = []

    def open(self, run_id, attempt, namespace):
        self.log.append(f"open {namespace.name}")
        if self.open_fails:
            raise RuntimeError("tool Pod not ready")
        session = ExecSession(run_id, attempt, Space(namespace.name, namespace.uid), {"inspect": Inspect()},
                              secrets=lambda: [], space_uid=lambda: namespace.uid)
        self.registry.register(session)
        return session

    def close(self, session):
        self.log.append("close")
        self.registry.unregister(session)


class Proposer:
    """The model: it calls exec through the registry while proposing."""

    def __init__(self, log, registry, *, calls=1, fail=False):
        self.log, self.registry, self.calls, self.fail, self.responses = log, registry, calls, fail, []

    def propose(self, task):
        self.log.append("propose")
        for n in range(self.calls):
            arguments = {"target_kind": "pod", "target_name": "chat", "lines": None}
            self.responses.append(self.registry.handle({
                "schema_version": 1, "run_id": RUN, "attempt": 1, "request_id": f"req-000{n}-flow",
                "args_sha256": args_sha256(arguments), "tool": "rehearsal.exec.inspect",
                "arguments": arguments}))
        if self.fail:
            raise RuntimeError("model failed")
        return PROPOSAL


def flow(**options):
    log, registry = [], ExecRegistry()
    phase = Phase(log, registry, **{k: v for k, v in options.items() if k != "proposer"})
    proposer = Proposer(log, registry, **options.get("proposer", {}))
    result = run_with_exec(Task(), proposer, phase, Lifecycle(log, "b"), Isolation(log), Verifier(log), 1)
    return result, log, phase, proposer, registry


def test_ai_space_is_removed_before_grading_space_is_created():
    result, log, phase, proposer, _ = flow()
    assert log == ["create a", "isolate rehearsal-a1", "open rehearsal-a1", "propose", "close",
                   "cleanup rehearsal-a1", "create b", "isolate rehearsal-b1", "verify rehearsal-b1",
                   "cleanup rehearsal-b1"]
    assert (result.state, result.proposal) == ("RECEIPT_UNVERIFIED", PROPOSAL)
    assert proposer.responses[0]["result_code"] == "OK"
    [trace] = phase.traces
    assert trace.space == Namespace("rehearsal-a1", "uid-a1") and trace.removed and trace.opened
    assert len(trace.events) == 1
    for event in trace.events:
        assert_valid("exec_event", event)


def test_unconfirmed_ai_space_cleanup_never_creates_grading_space():
    result, log, *_ = flow(cleaned=False)
    assert (result.state, result.reason) == ("NEEDS_HUMAN", "EXEC_SPACE_CLEANUP_UNCONFIRMED")
    assert "create b" not in log


def test_ai_space_isolation_failure_stops_before_the_model():
    result, log, *_ = flow(isolation_blocked=True)
    assert (result.state, result.reason) == ("BLOCK", "ISOLATION_BLOCKED")
    assert "propose" not in log and "create b" not in log and "cleanup rehearsal-a1" in log


def test_tool_pod_failure_degrades_to_refused_exec_calls():
    result, log, phase, proposer, _ = flow(open_fails=True)
    assert result.state == "RECEIPT_UNVERIFIED" and "create b" in log
    response = proposer.responses[0]
    assert (response["result_code"], response["reason"]) == ("REJECTED", "UNAVAILABLE")
    assert_valid("response", response)
    assert not phase.traces[0].opened and phase.traces[0].events == []


def test_model_failure_cleans_ai_space_and_does_not_grade():
    result, log, *_ = flow(proposer={"fail": True})
    assert (result.state, result.reason) == ("NEEDS_HUMAN", "PROPOSAL_UNAVAILABLE")
    assert "cleanup rehearsal-a1" in log and "create b" not in log


def test_session_is_closed_after_the_model_run():
    _, _, _, _, registry = flow()
    late = registry.handle({"schema_version": 1, "run_id": RUN, "attempt": 1, "request_id": "req-0009-late",
                            "args_sha256": "0" * 64, "tool": "rehearsal.exec.inspect", "arguments": {}})
    assert (late["result_code"], late["reason"]) == ("REJECTED", "UNAVAILABLE")


def test_registry_refuses_another_attempt_of_an_open_run():
    registry = ExecRegistry()
    session = ExecSession(RUN, 1, Space("rehearsal-a1", "uid-a1"), {}, secrets=lambda: [],
                          space_uid=lambda: "uid-a1")
    registry.register(session)
    response = registry.handle({"schema_version": 1, "run_id": RUN, "attempt": 2, "request_id": "req-0001-other",
                                "args_sha256": "0" * 64, "tool": "rehearsal.exec.probe", "arguments": {}})
    assert response["reason"] == "FOREIGN_ATTEMPT"
    assert_valid("response", response)
    with pytest.raises(ValueError):
        registry.register(session)


def test_exec_is_off_for_every_task_kind_by_default():
    config = ExecConfig()
    assert not config.enabled("bucket-policy") and not config.enabled("credential-migration")


def test_exec_off_keeps_the_existing_order():
    log = []
    proposer = Proposer(log, ExecRegistry(), calls=0)
    result = run_once(Task(), proposer, Lifecycle(log, "b"), Isolation(log), None, isolated_verifier=Verifier(log))
    assert result.state == "RECEIPT_UNVERIFIED"
    assert log == ["propose", "create b", "isolate rehearsal-b1", "verify rehearsal-b1", "cleanup rehearsal-b1"]
