"""A long-lived mock Executor and a killed platform process use separate databases."""

import json
import multiprocessing
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_cutover import BINDING, BINDINGS, COMMAND, EXPIRES, NOW, PRINCIPAL, SPEC
from test_executor import EXECUTION, response_value

from bravo_platform_events.change import ChangeApprovalService, CurrentChange
from bravo_platform_events.cutover import CurrentCutover, CutoverService
from bravo_platform_events.executor import ExecutorClient
from bravo_platform_events.plans import CollectionProof, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import (
    Analysis,
    Application,
    Change,
    Evidence,
    Gate,
    Integrity,
    Proposal,
    Run,
    ShiftApproval,
    Watch,
    WorkflowRecovery,
)

PLAN = SPEC.plan_id
CHANGE = Change(BINDING["diff_hash"], BINDING["image_digest"])
ANALYSIS = Analysis(SPEC.collection_scope_digest, "1" * 64, "2" * 64)
PROPOSAL = Proposal(ANALYSIS.inventory_digest, ANALYSIS.comparison_digest, CHANGE, True, True)
GATE = Gate(CHANGE, BINDING["gate_id"], BINDING["context_digest"], "PASS")
APPROVAL = ShiftApproval(GATE, "shift-approved", True, EXPIRES)
ROUTE = [
    "COLLECT",
    "ANALYZE",
    "PROPOSE",
    "REHEARSE",
    "GATE",
    "AWAIT_APPROVAL",
    "SHIFT",
    "CUTOVER",
    "WATCH",
    "DONE",
]


class Producers:
    """Owner fixtures. Integrity/WATCH refer to the actual remote execution claim."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.override = {}
        self.application = None

    def read(self, c, plan, previous, run):
        stage = plan["state"]
        if stage in self.override:
            return self.override[stage]
        result = {
            "ANALYZE": ANALYSIS,
            "PROPOSE": PROPOSAL,
            "REHEARSE": self.application,
            "GATE": GATE,
            "AWAIT_APPROVAL": APPROVAL,
        }.get(stage)
        if stage in ("CUTOVER", "WATCH"):
            with sqlite3.connect(self.ledger) as remote:
                command_id, epoch, execution = remote.execute(
                    "SELECT command_id,epoch,execution_id FROM effects WHERE plan_id=?", (PLAN,)
                ).fetchone()
            actual = Run(command_id, epoch, execution)
            result = (
                Integrity(actual, True)
                if stage == "CUTOVER"
                else Watch(actual, "proof-CUTOVER", "PASS", "PASS")
            )
        return Evidence(
            PLAN,
            SPEC.plan_digest,
            SPEC.target_environment_id,
            plan["version"],
            f"proof-{stage}",
            result,
        )


def service(store, client):
    return CutoverService(
        store,
        client,
        bindings=BINDINGS,
        clock=lambda: NOW,
        authority=lambda *a, **kw: CurrentCutover(BINDING, COMMAND, "rev1"),
    )


def collect(store):
    store.register_plan(SPEC, run_id="register", occurred_at=NOW)
    return store.complete_collection(
        CollectionProof(PLAN, SPEC.plan_digest, SPEC.collection_scope_digest, "collected", True),
        expected_version=1,
        command_key="collect",
        run_id="collector",
        occurred_at=NOW,
    )


def admit_change(store, change=CHANGE):
    binding = {k: v for k, v in BINDING.items() if k not in ("gate_id", "context_digest")}
    binding.update(diff_hash=change.diff_hash, image_digest=change.image_digest)
    candidate = CurrentChange(binding, "rehearsal-1", "verified-job", "revision-1")
    service = ChangeApprovalService(store, clock=lambda: NOW, authority=lambda *a, **kw: candidate)
    version = store.snapshot(PLAN)["version"]
    approval = service.approve(PRINCIPAL, PLAN, run_id=candidate.rehearsal_id, binding=binding,
                               expected_version=version, command_key="approve-change")
    claim, created = service.claim_application(PRINCIPAL, PLAN, approval_id=approval["approval_id"],
                                               command_key="apply-change")
    assert created
    return Application(change, claim["verification_id"], approval["approval_id"],
                       claim["application_id"], True, change.image_digest)


def to_shift(store, producers):
    collect(store)
    recovery = WorkflowRecovery(service(store, None), read_evidence=producers.read)
    for target in ROUTE[2:7]:
        if target == "GATE":
            producers.application = admit_change(store)
        assert recovery.resume_once(PRINCIPAL, PLAN) == target


def submit(cutover):
    receipt = cutover.approve(
        PRINCIPAL, PLAN, binding=BINDING, expected_version=7, command_key="cutover-approve"
    )
    return cutover.submit(PRINCIPAL, PLAN, approval_id=receipt["approval_id"], command_key="submit")


def run_worker(kind, location, origin, ledger, mode, reached):
    store = PostgresPlanStore(location) if kind == "postgres" else PlanStore(location)

    class PausableClient(ExecutorClient):
        def submit(self, *args, **kwargs):
            if mode == "before-send":
                reached.set()
                time.sleep(30)
                raise AssertionError("test did not kill worker before dispatch")
            return super().submit(*args, **kwargs)

    with PausableClient(origin, token="fixture-read", command_token="fixture-command") as client:
        cutover = service(store, client)
        if mode in ("before-send", "after-effect"):
            submit(cutover)
        else:
            recovery = WorkflowRecovery(cutover, read_evidence=Producers(ledger).read)
            # The replacement receives just a plan ID, never a command ID or approval key.
            for _ in range(3):
                state = recovery.resume_once(PRINCIPAL, PLAN)
                if state in ("DONE", "SAFE_HOLD"):
                    return
            raise AssertionError(state)


@contextmanager
def remote_executor(path, committed, reply):
    with sqlite3.connect(path) as remote:
        # Count all POST attempts, including duplicates with the same idempotency key.
        remote.executescript("""
            CREATE TABLE calls(method TEXT);
            CREATE TABLE effects(command_id TEXT PRIMARY KEY, plan_id TEXT, epoch INT, execution_id TEXT);
        """)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError):
                pass  # The submitting process can die before its response.

        def do_POST(self):
            assert self.path == "/v1/cutovers"
            command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert command == COMMAND
            with sqlite3.connect(path) as remote:
                remote.execute("INSERT INTO calls VALUES ('POST')")
            with sqlite3.connect(path) as remote:
                remote.execute(
                    "INSERT INTO effects VALUES (?,?,?,?)",
                    (self.headers["Idempotency-Key"], PLAN, command["epoch"], EXECUTION),
                )
            committed.set()
            assert reply.wait(30)
            self.send_json(202, response_value() | {"status": "DONE", "committed": True})

        def do_GET(self):
            with sqlite3.connect(path) as remote:
                remote.execute("INSERT INTO calls VALUES ('GET')")
                row = remote.execute("SELECT plan_id,epoch,execution_id FROM effects").fetchone()
            if row:
                value = response_value() | dict(
                    plan_id=row[0], epoch=row[1], execution_id=row[2], status="DONE", committed=True
                )
                if self.path == f"/v1/cutovers/{row[2]}":
                    return self.send_json(200, value)
            assert self.path == f"/v1/cutovers?plan_id={PLAN}&epoch={COMMAND['epoch']}"
            self.send_json(200, [value] if row else [])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        reply.set()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("crash", ["before-send", "after-effect"])
def test_kill_and_resume_from_plan_id(backend, tmp_path, crash):
    ctx = multiprocessing.get_context("spawn")
    reached = ctx.Event()
    committed, reply = threading.Event(), threading.Event()
    ledger = tmp_path / "remote.db"
    to_shift(backend.store, Producers(ledger))
    with remote_executor(ledger, committed, reply) as origin:
        args = (backend.kind, backend.conninfo if backend.cluster else backend.path, origin, ledger)
        worker = ctx.Process(target=run_worker, args=(*args, crash, reached))
        worker.start()
        try:
            assert (reached if crash == "before-send" else committed).wait(20)
            assert backend.reopen().snapshot(PLAN)["state"] == "CUTOVER"
        finally:
            worker.kill()
            worker.join(10)
            reply.set()
        assert worker.exitcode == -9
        replacement = ctx.Process(target=run_worker, args=(*args, "resume", reached))
        replacement.start()
        replacement.join(20)
        if replacement.is_alive():
            replacement.kill()
            replacement.join()
            pytest.fail("restarted worker stalled")
        assert replacement.exitcode == 0
        arrived = crash == "after-effect"
        path = ROUTE if arrived else [*ROUTE[:8], "SAFE_HOLD"]
        snapshot = backend.reopen().snapshot(PLAN)
        assert (snapshot["state"], snapshot["version"]) == (path[-1], len(path))
        events = backend.store.read_after(PLAN, None)
        assert [e["data"]["to"] for e in events if e["event_type"] == "state.changed"] == path
        assert (
            WorkflowRecovery(service(backend.reopen(), None)).resume_once(PRINCIPAL, PLAN)
            == path[-1]
        )
        with sqlite3.connect(ledger) as remote:
            assert remote.execute("SELECT count(*) FROM effects").fetchone()[0] == int(arrived)
            assert remote.execute("SELECT method FROM calls ORDER BY rowid").fetchall() == (
                [("POST",), ("GET",), ("GET",)] if arrived else [("GET",)]
            )


def test_rejects_missing_stale_or_failed_producer_evidence(backend, tmp_path):
    producers = Producers(tmp_path / "unused")
    store = backend.store
    collect(store)
    recovery = WorkflowRecovery(service(store, None), read_evidence=producers.read)
    for stage, bad in [
        ("ANALYZE", replace(ANALYSIS, scope_digest="0" * 64)),
        ("PROPOSE", replace(PROPOSAL, lint_passed=False)),
        ("REHEARSE", None),
        ("GATE", replace(GATE, verdict="UNKNOWN")),
        ("AWAIT_APPROVAL", replace(APPROVAL, expires_at=NOW)),
    ]:
        if stage == "REHEARSE":
            producers.application = admit_change(store)
            bad = replace(producers.application, deployed_digest="sha256:" + "0" * 64)
        before = store.snapshot(PLAN)
        assert before["state"] == stage
        good = producers.read(None, before, None, None)
        for invalid in (
            None,
            replace(good, version=good.version - 1),
            replace(good, plan_id="other"),
            replace(good, result=bad),
        ):
            producers.override[stage] = invalid
            with pytest.raises(EventError):
                recovery.resume_once(PRINCIPAL, PLAN)
            assert store.snapshot(PLAN) == before
        del producers.override[stage]
        recovery.resume_once(PRINCIPAL, PLAN)


@pytest.mark.parametrize("fault", ["no-claim", "approval_id", "application_id", "verification_id",
                                   "other-diff", "other-image", "older-plan"])
def test_deployment_must_match_the_plans_admitted_change(backend, tmp_path, fault):
    store = backend.store
    producers = Producers(tmp_path / "unused")
    collect(store)
    recovery = WorkflowRecovery(service(store, None), read_evidence=producers.read)
    assert recovery.resume_once(PRINCIPAL, PLAN) == "PROPOSE"
    assert recovery.resume_once(PRINCIPAL, PLAN) == "REHEARSE"
    if fault == "no-claim":
        observed = Application(CHANGE, "verified-job", "unrelated", "foreign-apply",
                               True, CHANGE.image_digest)
    else:
        approved_change = CHANGE
        if fault == "other-diff":
            approved_change = replace(CHANGE, diff_hash="sha256:" + "0" * 64)
        if fault == "other-image":
            approved_change = replace(CHANGE, image_digest="sha256:" + "0" * 64)
        if fault == "older-plan":
            # Model a claim from a prior visit to REHEARSE on this plan.
            with store._transaction(write=True, plan_id=PLAN) as c:
                c.execute("UPDATE plans SET version=3 WHERE plan_id=?", (PLAN,))
        admitted = admit_change(store, approved_change)
        if fault == "older-plan":
            with store._transaction(write=True, plan_id=PLAN) as c:
                c.execute("UPDATE plans SET version=4 WHERE plan_id=?", (PLAN,))
        observed = replace(admitted, change=CHANGE, deployed_digest=CHANGE.image_digest)
        if fault in ("approval_id", "application_id", "verification_id"):
            observed = replace(observed, **{fault: "different"})
    producers.application = observed
    before = store.snapshot(PLAN)
    with pytest.raises(EventError, match="workflow_condition_unmet"):
        recovery.resume_once(PRINCIPAL, PLAN)
    assert backend.reopen().snapshot(PLAN) == before


def test_committed_executor_still_requires_integrity_watch_and_retest(backend, tmp_path):
    ledger = tmp_path / "remote.db"
    producers = Producers(ledger)
    to_shift(backend.store, producers)
    reply = threading.Event()
    reply.set()
    with (
        remote_executor(ledger, threading.Event(), reply) as origin,
        ExecutorClient(origin, token="fixture-read", command_token="fixture-command") as client,
    ):
        cutover = service(backend.store, client)
        result = submit(cutover)
        run = Run(result["command_id"], COMMAND["epoch"], EXECUTION)
        recovery = WorkflowRecovery(cutover, read_evidence=producers.read)
        for stage, rejected in [
            ("CUTOVER", [Integrity(run, False), Integrity(replace(run, epoch=8), True)]),
            (
                "WATCH",
                [
                    Watch(run, "foreign-integrity", "PASS", "PASS"),
                    Watch(run, "proof-CUTOVER", "UNKNOWN", "PASS"),
                    Watch(run, "proof-CUTOVER", "PASS", "FAIL"),
                ],
            ),
        ]:
            snapshot = backend.store.snapshot(PLAN)
            good = producers.read(None, snapshot, None, None)
            for bad in rejected:
                producers.override[stage] = replace(good, result=bad)
                with pytest.raises(EventError, match="workflow_condition_unmet"):
                    recovery.resume_once(PRINCIPAL, PLAN)
                assert backend.reopen().snapshot(PLAN) == snapshot
            del producers.override[stage]
            recovery.resume_once(PRINCIPAL, PLAN)
        assert backend.store.snapshot(PLAN)["state"] == "DONE"


def test_revocation_before_checkpoint_write_rolls_back(backend):
    collect(backend.store)
    before = backend.store.snapshot(PLAN)
    cutover = service(backend.store, None)
    checked = []

    def access(c, principal, plan):
        checked.append(plan["version"])
        if len(checked) == 2:
            raise EventError("forbidden")

    cutover.check_access = access
    with pytest.raises(EventError, match="forbidden"):
        WorkflowRecovery(cutover).resume_once(PRINCIPAL, PLAN)
    assert checked == [2, 2]
    assert backend.store.snapshot(PLAN) == before


@pytest.mark.parametrize("table", ["plan_commands", "events"])
def test_interrupted_checkpoint_transaction_can_retry(backend, tmp_path, table):
    collect(backend.store)
    before = backend.store.snapshot(PLAN)
    producers = Producers(tmp_path / "unused")
    recovery = WorkflowRecovery(service(backend.store, None), read_evidence=producers.read)
    with backend.reject_insert(table), pytest.raises(backend.storage_error):
        recovery.resume_once(PRINCIPAL, PLAN)
    assert before == backend.reopen().snapshot(PLAN)
    assert recovery.resume_once(PRINCIPAL, PLAN) == "PROPOSE"
