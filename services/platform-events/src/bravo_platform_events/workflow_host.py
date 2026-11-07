"""Internal T10 workflow driver: the one process that advances plans.

Durable-execution pattern (the Agents SDK has no workflow runner; Temporal's
integration is the common reference): the plan row and `workflow:<version>`
checkpoints are the durable history, `WorkflowRecovery.resume_once` is the
idempotent step, and producer records (AI proposal, T15, T31, T19, Executor)
are activity results read by one reader per state. The AI is an activity behind
the PROPOSE reader; it never selects a state. No HTTP route, no client evidence.
"""

import json
import logging
import threading
from dataclasses import dataclass

from .store import EventError
from .workflow import STAGES, WorkflowRecovery

log = logging.getLogger(__name__)
HOST_SUBJECT = "workflow-host"
# FAILBACK is driven by resume_once too; every other state belongs to another owner.
DRIVEN = (*STAGES, "FAILBACK")


class StageReaders:
    """`read_evidence` for WorkflowRecovery, by state. An unwired state holds (fail closed).

    A preparer runs network reads for one plan version outside the plan lock and returns
    the reader for that version; the reader itself only checks locally under the lock.
    """

    def __init__(self, readers, *, preparers=None):
        preparers = preparers or {}
        if (set(readers) | set(preparers)) - set(STAGES) or set(readers) & set(preparers) or not all(
                map(callable, (*readers.values(), *preparers.values()))):
            raise ValueError("unknown_or_invalid_stage_reader")
        self.readers, self.preparers = dict(readers), dict(preparers)
        self._prepared = {}

    def prepare(self, plan_id, tenant_id, state, version):
        if state in self.preparers:
            self._prepared[(plan_id, version)] = self.preparers[state](plan_id, tenant_id, version)

    def __call__(self, c, plan, previous, run):
        reader = self._prepared.pop((plan["plan_id"], plan["version"]), None) or self.readers.get(plan["state"])
        if reader is None:
            raise EventError("workflow_reader_unwired")
        return reader(c, plan, previous, run)


@dataclass(frozen=True)
class Step:
    plan_id: str
    before: str
    after: str
    hold: str | None = None  # EventError code: waiting on a producer or a refused proof


class WorkflowHost:
    def __init__(self, recovery: WorkflowRecovery, *, subject=HOST_SUBJECT):
        if not isinstance(recovery, WorkflowRecovery):
            raise ValueError("workflow_recovery_required")
        self.recovery, self.subject = recovery, subject
        self.store = recovery.cutover.store
        self._last = {}

    def active(self):
        marks = ",".join("?" * len(DRIVEN))
        with self.store._transaction() as c:
            rows = c.execute(f"SELECT plan_id,tenant_id,state,version FROM plans WHERE state IN ({marks}) "
                             "ORDER BY plan_id", DRIVEN).fetchall()
        return [(row["plan_id"], row["tenant_id"], row["state"], row["version"]) for row in rows]

    def _version(self, plan_id):
        with self.store._transaction() as c:
            return c.execute("SELECT version FROM plans WHERE plan_id=?", (plan_id,)).fetchone()["version"]

    def advance(self, plan_id, tenant_id, state, version):
        """Step one plan until it stops moving. Each step is its own durable transition."""
        # An operator identity so the deployment's OperatorAccess.check_read applies; it needs only
        # `read` on the plan (approvals and SHIFT stay with people).
        from .workflow_stages import host_principal

        principal = host_principal(self.subject, tenant_id)
        readers = self.recovery.read_evidence
        start = state
        for _ in range(len(DRIVEN) + 1):  # ponytail: bound only; a cycle would be a workflow bug
            try:
                if isinstance(readers, StageReaders):
                    readers.prepare(plan_id, tenant_id, state, version)  # outside the plan lock
                after = self.recovery.resume_once(principal, plan_id)
            except EventError as error:
                return Step(plan_id, start, state, error.code)
            except Exception as error:  # noqa: BLE001 - e.g. T19 GateError: never stall without a reason
                from .startup import reason

                log.exception("workflow step refused", extra={"plan_id": plan_id})
                return Step(plan_id, start, state, "producer_refused:" + reason(error))
            if after == state:
                break
            state, version = after, self._version(plan_id)
        return Step(plan_id, start, state)

    def sweep(self):
        steps = []
        for plan_id, tenant_id, state, version in self.active():
            try:
                step = self.advance(plan_id, tenant_id, state, version)
            except Exception:  # storage or producer outage: keep other plans moving
                log.exception("workflow step failed", extra={"plan_id": plan_id})
                continue
            steps.append(step)
            seen = (step.after, step.hold)
            if step.before != step.after or self._last.get(plan_id) != seen:
                log.info(json.dumps({"event": "workflow.step", **step.__dict__}, sort_keys=True))
            self._last[plan_id] = seen
        return steps

    def run(self, stop: threading.Event, interval=15.0):
        # ponytail: periodic sweep; add a state.changed/producer wakeup when latency matters.
        while True:
            self.sweep()
            if stop.wait(interval):
                return
