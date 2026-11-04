"""FAILBACK approval and single dispatch (T20 spec commit 3). Synthetic authority fixture only.

The plan is seeded as a completed cutover in WATCH: one cutover command observed as committed DONE
and its matched integrity checkpoint. No production authority, gate or Edge is minted here.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import httpx
import pytest
from test_executor import response_value

from bravo_platform_events.cutover import CutoverService, _key, _put
from bravo_platform_events.executor import ExecutionSnapshot, ExecutionStep, ExecutorClient
from bravo_platform_events.failback import CurrentFailback, FailbackIntegrity, FailbackService
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import Evidence, Integrity, Run, Watch, WorkflowRecovery

NOW = "2026-11-04T10:00:00+00:00"
EXPIRES = "2026-11-04T10:10:00+00:00"
PRINCIPAL = Principal("operator", "tenant-demo")
SPEC = PlanSpec("plan-demo", "tenant-demo", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
BINDINGS = {"env-aws": "cutover/source", "env-onprem": "cutover/target"}
CUTOVER_COMMAND = "11111111-1111-4111-8111-111111111111"
CUTOVER_EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-abcdefabcdef"
FAILBACK_EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-abcdefabcdef"
RECEIPT = "r-cutover-55"
BINDING = dict(plan_id=SPEC.plan_id, plan_digest=SPEC.plan_digest, source_environment_id="env-onprem",
               target_environment_id="env-aws", cutover_execution_id=CUTOVER_EXECUTION, cutover_epoch=7,
               integrity_receipt_id=RECEIPT, expires_at=EXPIRES)
COMMAND = dict(plan_id=SPEC.plan_id, epoch=8,
               source=dict(environment_id="env-onprem", connection_ref="cutover/target"),
               target=dict(environment_id="env-aws", connection_ref="cutover/source"),
               write_pause_budget_ms=30000, deadline=EXPIRES)


def seed_completed_cutover(backend, *, observed="DONE", committed=True, checkpoint=True):
    """Test-only prerequisite seed: no runtime route can create these records."""
    with backend.admin() as c:
        c.execute("UPDATE plans SET state='WATCH', version=5 WHERE plan_id='plan-demo'")
    snapshot = ExecutionSnapshot(CUTOVER_EXECUTION, SPEC.plan_id, 7, observed, committed,
                                 (ExecutionStep("precheck", "succeeded", 1),), NOW, NOW)
    receipt = dict(command_id=CUTOVER_COMMAND, approval_id="cutover-approval", command=dict(epoch=7))
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        _put(c, SPEC.plan_id, _key("command", CUTOVER_COMMAND), {}, receipt)
        _put(c, SPEC.plan_id, _key("epoch", "7"), {}, receipt)
        _put(c, SPEC.plan_id, f"{_key('observation', CUTOVER_COMMAND)}-{1:012d}", {},
             dict(index=1, outcome="OBSERVED", reason=None, snapshot=asdict(snapshot)))
        if checkpoint:
            evidence = Evidence(SPEC.plan_id, SPEC.plan_digest, "env-onprem", 3, RECEIPT,
                                Integrity(Run(CUTOVER_COMMAND, 7, CUTOVER_EXECUTION), True))
            _put(c, SPEC.plan_id, "workflow:3", {}, asdict(evidence))


class Fixture:
    def __init__(self, backend):
        self.backend = backend
        self.now = NOW
        self.context = CurrentFailback(dict(BINDING), json.loads(json.dumps(COMMAND)), "authority-rev1")
        self.allowed = True
        self.requests = []
        self.value = response_value() | dict(execution_id=FAILBACK_EXECUTION, epoch=8)
        self.fail_post = False
        self.empty_get = False
        self.fail_get = False
        self.integrity = "match"  # match | missing | other_run | unmatched
        self.edge_calls = []
        self.edge_result = True
        self.during_edge = None
        backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
        seed_completed_cutover(backend)
        self.client = ExecutorClient("https://executor.example.invalid", token="fixture-token",
                                     transport=httpx.MockTransport(self.handle))

    def authority(self, c, principal, plan, *, now, approved_by, approval_id):
        if not self.allowed:
            raise RuntimeError("PRIVATE_PERMISSION_ERROR")
        return self.context

    def handle(self, request):
        self.requests.append(request)
        if request.method == "POST":
            # The durable claim and the WATCH -> FAILBACK event exist before any remote effect.
            assert self.backend.reopen().snapshot(SPEC.plan_id)["state"] == "FAILBACK"
            if self.fail_post:
                raise httpx.ReadTimeout("PRIVATE_RESPONSE_LOSS")
            return httpx.Response(202, json=self.value)
        if self.fail_get:
            raise httpx.ReadTimeout("PRIVATE_GET_LOSS")
        if request.url.path.endswith(CUTOVER_EXECUTION):  # the completed cutover, as seeded
            return httpx.Response(200, json=response_value() | dict(
                execution_id=CUTOVER_EXECUTION, epoch=7, status="DONE", committed=True, created_at=NOW,
                updated_at=NOW, steps=[dict(step="precheck", status="succeeded", elapsed_ms=1)]))
        if self.empty_get:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[self.value] if request.url.path == "/v1/cutovers" else self.value)

    def read_integrity(self, c, plan, *, command_id, epoch, execution_id):
        if self.integrity == "missing":
            raise LookupError("PRIVATE_NOT_FOUND")
        if self.integrity == "other_run":  # e.g. the reader returned the cutover's receipt
            return FailbackIntegrity(RECEIPT, CUTOVER_COMMAND, 7, CUTOVER_EXECUTION, True)
        return FailbackIntegrity("r-failback-56", command_id, epoch, execution_id, self.integrity == "match")

    def restore_origin(self, **kwargs):
        self.edge_calls.append(kwargs)
        if self.during_edge:
            self.during_edge()
        if isinstance(self.edge_result, BaseException):
            raise self.edge_result
        return self.edge_result

    def service(self, **kwargs):
        return FailbackService(self.backend.reopen(), self.client, bindings=BINDINGS,
                               authority=kwargs.get("authority", self.authority),
                               read_integrity=self.read_integrity, edge=self, clock=lambda: self.now)

    def advance(self, command_id):
        return self.service().advance(PRINCIPAL, SPEC.plan_id, command_id)

    def approve(self, **changes):
        args = dict(binding=BINDING, expected_version=5, command_key="approve-1")
        return self.service().approve(PRINCIPAL, SPEC.plan_id, **(args | changes))

    def submit(self, approval, **changes):
        args = dict(approval_id=approval["approval_id"], command_key="submit-1")
        return self.service().submit(PRINCIPAL, SPEC.plan_id, **(args | changes))

    def state(self):
        return self.backend.reopen().snapshot(SPEC.plan_id)["state"]

    def events(self, kind):
        return [e for e in self.backend.store.read_after(SPEC.plan_id, None) if e["event_type"] == kind]


@pytest.fixture
def fixture(backend):
    f = Fixture(backend)
    yield f
    f.client.close()


def test_approval_then_single_reversed_post(fixture):
    f = fixture
    approval = f.approve()
    assert f.approve() == approval
    assert approval["approval_kind"] == "failback" and approval["actor_id"] == "operator"
    receipt = f.submit(approval)
    assert receipt["snapshot"]["execution_id"] == FAILBACK_EXECUTION
    assert [r.method for r in f.requests] == ["POST"]
    assert json.loads(f.requests[0].content) == COMMAND  # onprem -> aws, epoch above the cutover
    assert f.requests[0].headers["Idempotency-Key"] == receipt["command_id"]
    assert f.state() == "FAILBACK"
    decided = f.events("approval.decided")[-1]["data"]
    assert decided["approval_kind"] == "failback" and decided["binding"] == BINDING
    assert f.events("state.changed")[-1]["data"] == {"from": "WATCH", "to": "FAILBACK"}


@pytest.mark.parametrize("field,value", [
    ("plan_digest", "f" * 64), ("source_environment_id", "env-aws"), ("target_environment_id", "env-onprem"),
    ("cutover_execution_id", FAILBACK_EXECUTION), ("cutover_epoch", 6), ("integrity_receipt_id", "r-other"),
])
def test_binding_must_name_the_completed_cutover(fixture, field, value):
    f = fixture
    f.context = replace(f.context, binding=BINDING | {field: value})
    with pytest.raises(EventError, match="binding_mismatch"):
        f.approve(binding=BINDING | {field: value})
    assert not f.requests and not f.events("approval.decided")


@pytest.mark.parametrize("state", ["SHIFT", "CUTOVER", "SAFE_HOLD", "FAILBACK", "DONE"])
def test_only_a_watched_plan_can_be_rolled_back(fixture, state):
    f = fixture
    with f.backend.admin() as c:
        c.execute("UPDATE plans SET state=? WHERE plan_id='plan-demo'".replace("?", f"'{state}'"))
    with pytest.raises(EventError, match="transition_not_allowed"):
        f.approve()


@pytest.mark.parametrize("seed", [dict(observed="RUNNING", committed=False), dict(checkpoint=False)])
def test_no_committed_and_matched_cutover_means_no_failback(backend, seed):
    backend.store.register_plan(SPEC, run_id="fixture", occurred_at=NOW)
    seed_completed_cutover(backend, **seed)
    service = FailbackService(backend.reopen(), None, bindings=BINDINGS,
                              authority=lambda *a, **k: CurrentFailback(dict(BINDING), dict(COMMAND), "rev"),
                              clock=lambda: NOW)
    with pytest.raises(EventError, match="cutover_record_unavailable"):
        service.approve(PRINCIPAL, SPEC.plan_id, binding=BINDING, expected_version=5, command_key="k")


@pytest.mark.parametrize("command,code", [
    (COMMAND | dict(source=COMMAND["target"], target=COMMAND["source"]), "unregistered_database_pair"),
    (COMMAND | dict(source=dict(environment_id="env-onprem", connection_ref="other/ref")),
     "unregistered_database_pair"),
    (COMMAND | dict(epoch=7), "epoch_not_increasing"),
    (COMMAND | dict(epoch=6), "epoch_not_increasing"),
    (COMMAND | dict(deadline="2026-11-04T10:20:00+00:00"), "invalid_command"),
])
def test_only_the_reversed_registered_pair_with_a_new_epoch(fixture, command, code):
    f = fixture
    f.context = replace(f.context, command=command)
    with pytest.raises(EventError, match=code):
        f.approve()
    assert not f.requests


def test_default_authority_denies_and_masks_failure(fixture):
    f = fixture
    service = FailbackService(f.backend.reopen(), f.client, bindings=BINDINGS, clock=lambda: NOW)
    with pytest.raises(EventError, match="authority_unavailable"):
        service.approve(PRINCIPAL, SPEC.plan_id, binding=BINDING, expected_version=5, command_key="k")
    f.allowed = False
    with pytest.raises(EventError, match="authority_unavailable"):
        f.approve()


def test_foreign_tenant_never_reaches_authority_or_executor(fixture):
    f = fixture
    with pytest.raises(EventError, match="forbidden"):
        f.service().approve(Principal("operator", "tenant-other"), SPEC.plan_id, binding=BINDING,
                            expected_version=5, command_key="k")
    assert not f.requests


def test_response_loss_only_get_and_hold_is_not_cleared(fixture):
    f = fixture
    approval = f.approve()
    f.fail_post = True
    assert f.submit(approval)["outcome"] == "UNKNOWN"
    assert f.state() == "SAFE_HOLD"
    assert f.events("state.changed")[-1]["data"] == {"from": "FAILBACK", "to": "SAFE_HOLD"}
    f.value.update(status="DONE", committed=True)
    assert f.submit(approval)["snapshot"]["remote_status"] == "DONE"
    assert f.state() == "SAFE_HOLD"
    assert [r.method for r in f.requests] == ["POST", "GET"]


def test_crash_after_durable_claim_never_dispatches_on_retry(fixture):
    f = fixture
    approval = f.approve()
    record, created = f.service()._prepare(PRINCIPAL, SPEC.plan_id, approval["approval_id"], "submit-1")
    assert created and f.state() == "FAILBACK"
    f.empty_get = True
    result = f.submit(approval)
    assert result["command_id"] == record["command_id"] and result["outcome"] == "UNKNOWN"
    assert [r.method for r in f.requests] == ["GET"]  # POST 0: the claim was never sent
    assert f.state() == "SAFE_HOLD"


def test_concurrent_submit_sends_one_post(fixture):
    f = fixture
    approval = f.approve()
    barrier = Barrier(6)

    def submit(_):
        barrier.wait(timeout=10)
        return f.submit(approval)

    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(submit, range(6)))
    assert len({r["command_id"] for r in receipts}) == 1
    assert sum(r.method == "POST" for r in f.requests) == 1


@pytest.mark.parametrize("table", ["events", "plan_commands"])
def test_store_failure_rolls_back_before_dispatch(fixture, table):
    f = fixture
    approval = f.approve()
    with f.backend.reject_insert(table), pytest.raises(f.backend.storage_error):
        f.submit(approval)
    assert not f.requests and f.state() == "WATCH"
    assert f.submit(approval)["outcome"] == "OBSERVED"


def test_consumed_approval_and_key_namespaces_are_separate(fixture):
    f = fixture
    approval = f.approve()
    receipt = f.submit(approval)
    with pytest.raises(EventError, match="command_key_conflict"):
        f.submit(approval, approval_id="other-approval")
    # A cutover approval ID is not a failback approval, and the cutover journal cannot see this command.
    with pytest.raises(EventError, match="approval_unavailable"):
        f.service()._prepare(PRINCIPAL, SPEC.plan_id, "cutover-approval", "submit-2")
    cutover = CutoverService(f.backend.reopen(), f.client, bindings=BINDINGS, clock=lambda: NOW)
    with pytest.raises(EventError, match="command_not_found"):
        cutover.reconcile(PRINCIPAL, SPEC.plan_id, receipt["command_id"])


def test_stored_committed_done_is_not_held_by_a_later_failed_get(fixture):
    f = fixture
    f.value.update(status="DONE", committed=True)
    f.edge_result = False  # DB is back on the origin, the Edge is not confirmed yet
    approval = f.approve()
    receipt = f.submit(approval)
    assert receipt["snapshot"]["remote_status"] == "DONE" and receipt["failback"] == "edge_pending"
    f.fail_get = True
    assert f.service().reconcile(PRINCIPAL, SPEC.plan_id, receipt["command_id"])["outcome"] == "UNKNOWN"
    assert f.state() == "FAILBACK"  # not SAFE_HOLD: only the Edge remains


# After a committed DONE: evidence, Edge restore and completion (spec commit 4) -----------------

def done(f):
    f.value.update(status="DONE", committed=True)
    approval = f.approve()
    return approval, f.submit(approval)


def records(f):
    return [e["data"] for e in f.events("failback.recorded")]


def test_rollback_completes_after_bound_evidence_and_confirmed_edge(fixture):
    f = fixture
    f.now = "2026-11-04T10:00:00+00:00"
    approval, receipt = done(f)
    assert receipt["failback"] == "rolled_back" and f.state() == "DONE_ROLLED_BACK"
    assert f.edge_calls == [dict(plan_id=SPEC.plan_id, command_id=receipt["command_id"],
                                 execution_id=FAILBACK_EXECUTION, epoch=8)]
    pending, finished = records(f)
    assert pending["status"] == "edge_pending" and pending["integrity_receipt_id"] == "r-failback-56"
    assert finished == pending | dict(status="rolled_back", finished_at=f.now, recovery_ms=0)
    assert finished["approved_by"] == "operator" and finished["approval_id"] == approval["approval_id"]
    assert f.events("state.changed")[-1]["data"] == {"from": "FAILBACK", "to": "DONE_ROLLED_BACK"}
    assert [r.method for r in f.requests] == ["POST"]


def test_edge_failure_waits_and_retries_only_the_edge(fixture):
    f = fixture
    f.edge_result = RuntimeError("PRIVATE_CLOUDFLARE_ERROR")
    _, receipt = done(f)
    assert receipt["failback"] == "edge_pending" and f.state() == "FAILBACK"
    f.edge_result = False  # reachable but not confirmed yet
    assert f.advance(receipt["command_id"]) == "edge_pending"
    f.edge_result, f.now = True, "2026-11-04T10:00:09+00:00"
    assert f.advance(receipt["command_id"]) == "rolled_back" and f.state() == "DONE_ROLLED_BACK"
    assert [r["status"] for r in records(f)] == ["edge_pending", "rolled_back"]
    assert records(f)[-1]["recovery_ms"] == 9000  # dispatch claim -> confirmed Edge (D6)
    assert [r.method for r in f.requests] == ["POST"]  # the DB command was never sent again
    assert len({tuple(sorted(c.items())) for c in f.edge_calls}) == 1  # one idempotent request


class Crash(BaseException):
    """Process loss after the Edge confirmed but before completion was stored."""


def test_crash_after_edge_success_retries_the_same_request_and_completes_once(fixture):
    f = fixture
    f.edge_result = Crash()
    with pytest.raises(Crash):
        done(f)
    assert f.state() == "FAILBACK" and [r["status"] for r in records(f)] == ["edge_pending"]
    f.edge_result = True
    command_id = f.edge_calls[0]["command_id"]
    assert f.advance(command_id) == "rolled_back"
    assert f.advance(command_id) == "rolled_back"
    assert f.edge_calls[0] == f.edge_calls[1] and len(f.edge_calls) == 2
    assert [r["status"] for r in records(f)] == ["edge_pending", "rolled_back"]


def test_concurrent_advance_completes_once(fixture):
    f = fixture
    f.edge_result = False
    _, receipt = done(f)
    f.edge_result = True
    barrier = Barrier(4)

    def advance(_):
        barrier.wait(timeout=10)
        return f.advance(receipt["command_id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(advance, range(4))) == {"rolled_back"}
    assert [r["status"] for r in records(f)].count("rolled_back") == 1
    assert [e["data"]["to"] for e in f.events("state.changed")].count("DONE_ROLLED_BACK") == 1


def test_hold_before_or_during_the_edge_call_wins(fixture):
    f = fixture
    approval = f.approve()
    f.fail_post = True
    receipt = f.submit(approval)  # UNKNOWN before any DONE: SAFE_HOLD
    assert f.state() == "SAFE_HOLD"
    assert f.advance(receipt["command_id"]) == "not_failback" and not f.edge_calls


def test_hold_stored_while_the_edge_call_is_in_flight_is_not_overwritten(fixture):
    f = fixture

    def operator_hold():
        with f.backend.admin() as c:
            c.execute("UPDATE plans SET state='SAFE_HOLD' WHERE plan_id='plan-demo'")

    f.during_edge = operator_hold
    _, receipt = done(f)
    assert receipt["failback"] == "not_failback" and f.state() == "SAFE_HOLD"
    assert [r["status"] for r in records(f)] == ["edge_pending"]


@pytest.mark.parametrize("integrity,result,state", [
    ("missing", "evidence_pending", "FAILBACK"),
    ("other_run", "held", "SAFE_HOLD"),
    ("unmatched", "held", "SAFE_HOLD"),
])
def test_no_edge_call_without_evidence_bound_to_this_run(fixture, integrity, result, state):
    f = fixture
    f.integrity = integrity
    _, receipt = done(f)
    assert receipt["failback"] == result and f.state() == state
    assert not f.edge_calls and not records(f)


def test_abort_returns_to_watch_and_a_retry_needs_a_new_approval_and_epoch(fixture):
    f = fixture
    f.value.update(status="ABORT_PRE_WRITE", committed=False)
    approval = f.approve()
    receipt = f.submit(approval)
    assert receipt["failback"] == "aborted" and f.state() == "WATCH"
    aborted = records(f)[-1]
    assert aborted["status"] == "aborted" and aborted["execution_id"] == FAILBACK_EXECUTION
    assert f.events("state.changed")[-1]["data"] == {"from": "FAILBACK", "to": "WATCH"}
    with pytest.raises(EventError, match="approval_unavailable"):  # the approval stays consumed
        f.service()._prepare(PRINCIPAL, SPEC.plan_id, approval["approval_id"], "submit-2")
    with pytest.raises(EventError, match="epoch_not_increasing"):  # 7 -> 8 (aborted) -> 8 again
        f.approve(command_key="approve-2", expected_version=7)
    f.context = replace(f.context, command=COMMAND | dict(epoch=9))
    f.value.update(status="DONE", committed=True, epoch=9, execution_id="cccccccc-cccc-4ccc-8ccc-abcdefabcdef")
    second = f.approve(command_key="approve-2", expected_version=7)
    assert f.submit(second, command_key="submit-2")["failback"] == "rolled_back"
    assert [json.loads(r.content)["epoch"] for r in f.requests if r.method == "POST"] == [8, 9]


# Restart recovery (spec commit 5) --------------------------------------------------------------

def recovery(f):
    cutover = CutoverService(f.backend.reopen(), f.client, bindings=BINDINGS, clock=lambda: f.now)

    def read_evidence(c, plan, previous, run):
        assert plan["state"] == "WATCH"
        return Evidence(SPEC.plan_id, SPEC.plan_digest, SPEC.target_environment_id, plan["version"], "r-watch",
                        Watch(run, previous["receipt_id"], "PASS", "PASS"))

    return WorkflowRecovery(cutover, read_evidence=read_evidence, failback=f.service())


def test_resume_after_claim_before_post_only_gets(fixture):
    f = fixture
    approval = f.approve()
    f.service()._prepare(PRINCIPAL, SPEC.plan_id, approval["approval_id"], "submit-1")
    f.empty_get = True
    assert recovery(f).resume_once(PRINCIPAL, SPEC.plan_id) == "SAFE_HOLD"
    assert [r.method for r in f.requests] == ["GET"] and not f.edge_calls


def test_resume_after_stored_done_calls_only_the_edge(fixture):
    f = fixture
    f.edge_result = False
    _, receipt = done(f)
    assert receipt["failback"] == "edge_pending"
    sent = len(f.requests)
    f.edge_result = True
    assert recovery(f).resume_once(PRINCIPAL, SPEC.plan_id) == "DONE_ROLLED_BACK"
    assert len(f.requests) == sent  # no GET, no POST: the DB part is final
    assert len(f.edge_calls) == 2 and f.edge_calls[0] == f.edge_calls[1]


def test_two_aborted_failbacks_then_watch_still_completes(fixture):
    f = fixture
    f.value.update(status="ABORT_PRE_WRITE", committed=False)
    for attempt, (epoch, execution) in enumerate(((8, FAILBACK_EXECUTION),
                                                  (9, "dddddddd-dddd-4ddd-8ddd-abcdefabcdef"))):
        f.context = replace(f.context, command=COMMAND | dict(epoch=epoch))
        f.value.update(epoch=epoch, execution_id=execution)  # the Executor names each run anew
        approval = f.approve(command_key=f"approve-{attempt}", expected_version=5 + 2 * attempt)
        assert f.submit(approval, command_key=f"submit-{attempt}")["failback"] == "aborted"
    assert f.state() == "WATCH" and f.backend.reopen().snapshot(SPEC.plan_id)["version"] == 9
    assert recovery(f).resume_once(PRINCIPAL, SPEC.plan_id) == "DONE"
