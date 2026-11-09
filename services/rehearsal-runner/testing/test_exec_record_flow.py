"""Signed attempt record with and without rehearsal.exec (real T10 Admission type).

cd ../platform-events
PYTHONPATH=../rehearsal-runner/src:../rehearsal-runner/tests uv run --frozen --no-default-groups --with pytest --with jsonschema \
  pytest -p no:cacheprovider ../rehearsal-runner/testing/test_exec_record_flow.py
"""

from bravo_platform_events.rehearsal import Admission

from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.attempt_store import (
    AttemptStore,
    PlanBinding,
    RecordedAttemptSender,
    RecordedEvidence,
)
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.exec_contract import args_sha256, inspect_record
from rehearsal_runner.exec_flow import ExecRegistry
from rehearsal_runner.exec_session import ExecSession, Outcome, Space
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.retry import AttemptPorts
from rehearsal_runner.runner import Namespace, ProposalRef

KEY = b"k" * 32
STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
          "ready_replicas": None, "restarts": 0, "events": None}


class Inspect:
    def validate(self, arguments):
        return inspect_record(arguments)

    def run(self, arguments, deadline):
        return Outcome(STATUS, b"ok\n")


class Space_:
    """Lifecycle and isolation for one namespace label."""

    def __init__(self, name, uid):
        self.namespace = Namespace(name, uid)

    def create(self, run_id):
        return self.namespace

    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-" + namespace.uid)

    def cleanup(self, namespace):
        return True


class Phase:
    def __init__(self, registry):
        space = Space_("rehearsal-ai", "uid-a")
        self.lifecycle = self.isolation = space
        self.registry, self.traces, self.journal = registry, [], None

    def open(self, run_id, attempt, namespace):
        session = ExecSession(run_id, attempt, Space(namespace.name, namespace.uid), {"inspect": Inspect()},
                              secrets=lambda: [], space_uid=lambda: namespace.uid, journal=self.journal)
        self.registry.register(session)
        return session

    def close(self, session):
        self.registry.unregister(session)


class Ports:
    attempt = 1

    def __init__(self, registry, exec_on):
        self.registry, self.exec_on = registry, exec_on
        self.grading = Space_("rehearsal-grade", "uid-b")

    def prepare(self, attempt, feedback_ref):
        phase = Phase(self.registry) if self.exec_on else None
        return AttemptPorts(self, self.grading, self.grading, self, exec_phase=phase)

    def propose(self, task):
        if self.exec_on:
            arguments = {"target_kind": "pod", "target_name": "chat", "lines": None}
            self.registry.handle({"schema_version": 1, "run_id": task.run_id, "attempt": 1,
                                  "request_id": "req-0001-flow", "args_sha256": args_sha256(arguments),
                                  "tool": "rehearsal.exec.inspect", "arguments": arguments})
        return ProposalRef(task.run_id, task.source_sha, "c" * 64)

    def verify_isolated(self, proposal, namespace, isolation):
        return None  # no product verifier here; the attempt needs a human


class Planner:
    def __init__(self, ports):
        self.ports = ports

    def binding(self, admission):
        return PlanBinding("d" * 64, "env-onprem")

    def factory(self, admission, task):
        return self.ports


def attempt(tmp_path, exec_on):
    task = prepare_bucket_policy_task("run-1", '{"Statement":[]}', "fixture-bucket", source_sha="a" * 40)
    admission = Admission("plan-1", "tenant-1", 1, "bucket", "run-1", 1, "a" * 40, logical_task_digest(task))
    store = AttemptStore(tmp_path, KEY)
    RecordedAttemptSender(store, Planner(Ports(ExecRegistry(), exec_on))).submit_once(admission, task)
    return store, admission, store.read("run-1", 1)


def test_exec_events_and_space_a_are_signed_into_the_attempt_record(tmp_path):
    from exec_schema import assert_valid

    store, admission, record = attempt(tmp_path, exec_on=True)
    assert record["exec_space"] == {"namespace": "rehearsal-ai", "created_uid": "uid-a", "absence_confirmed": True}
    assert record["exec_events_status"] == "COMPLETE"
    [event] = record["exec_events"]
    assert (event["seq"], event["action"], event["result_code"], event["target_name"]) == (1, "inspect", "OK", "chat")
    assert_valid("exec_record", {k: record[k] for k in ("exec_events_status", "exec_events")})
    assert RecordedEvidence(store).exec_events(admission) == {"exec_events_status": "COMPLETE",
                                                              "exec_events": record["exec_events"]}


def test_attempt_without_exec_keeps_the_record_unchanged(tmp_path):
    store, admission, record = attempt(tmp_path, exec_on=False)
    assert not any(key.startswith("exec_") for key in record)
    assert RecordedEvidence(store).exec_events(admission) == {"exec_events_status": "OFF"}
