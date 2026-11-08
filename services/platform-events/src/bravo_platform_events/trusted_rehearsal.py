"""Compose existing T10 and T15 services inside one explicitly trusted host."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore

from rehearsal_runner.approval_handoff import submit_candidate
from rehearsal_runner.bucket_planner import build_bucket_host

from .change import change_key
from .change_intake import Candidate, ChangeIntake
from .cutover import _get, _put
from .operator_access import OperatorAccess, create_operator_cutover_app
from .operator_identity import OperatorTokenVerifier
from .store import EventError


class AttemptDispatcher:
    """One worker plus one pending claim, in this process; never retries work.

    T15 durably claims before calling this port. Saturation, failure, or shutdown
    therefore leaves an unconfirmed claim for an operator, never a new submit.
    """

    def __init__(self):
        self._capacity = BoundedSemaphore(2)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t15-attempt")

    def __call__(self, work):
        if not self._capacity.acquire(blocking=False):
            raise EventError("rehearsal_dispatch_unavailable")
        try:
            future = self._pool.submit(work)
        except Exception:
            self._capacity.release()
            raise EventError("rehearsal_dispatch_unavailable") from None
        future.add_done_callback(lambda _future: self._capacity.release())

    def close(self):
        self._pool.shutdown(wait=True, cancel_futures=True)


class OperatorHoldPolicy:
    """A separate, current human permission, checked within ChangeIntake's lock.

    The credential is a human access token, not a principal supplied by a model
    or the T15 sender. Administrative grants use the existing trusted host port.
    """

    def __init__(self, access, verifier: OperatorTokenVerifier):
        self.access, self.verifier = access, verifier

    def __call__(self, connection, credential, plan_id, verification_id, decision_ref):
        actor = self.verifier.verify(credential)
        plan = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if actor is None or plan is None:
            raise EventError("forbidden")
        self.access.require(connection, actor, dict(plan), "hold_rehearsal")
        key = change_key("hold-decision", decision_ref)
        decision = dict(subject_id=actor.subject_id, tenant_id=actor.tenant_id,
                        verification_id=verification_id, decision_ref=decision_ref)
        old = _get(connection, plan_id, key)
        if old is not None and old[0] != decision:
            raise EventError("command_key_conflict")
        if old is None:
            _put(connection, plan_id, key, decision, decision)
        return True


@dataclass(frozen=True)
class TrustedHostPorts:
    """Reviewed deployment code supplies actual ports; no fixtures/default PASS."""

    executor: object = field(repr=False)
    bindings: dict
    catalog: object = field(repr=False)
    planner: object = field(repr=False)
    cutover_authority: object = field(repr=False)
    change_authority_wrapper: object = field(default=None, repr=False)
    # T20 failback: upstream authority and integrity reader, both optional (absent = no failback route).
    failback_authority: object = field(default=None, repr=False)
    read_integrity: object = field(default=None, repr=False)
    edge: object = field(default=None, repr=False)  # T17 FailbackEdge over the one LB journal
    iac: object = field(default=None, repr=False)  # T44 platform repository port (iac_plans), optional
    delegation: bool = False  # T49 pre-delegated approvals: the route and this process's cutover/failback sweep
    delegated_shift: object = field(default=None, repr=False)  # T49: (store, delegations) -> DelegatedShift
    # T49: RehearsalService.first_run (the stored AI Proposal's run ID). Without it a delegated REHEARSE of a
    # plan with an initial proposal holds at initial_rehearsal_reader_required.
    first_run: object = field(default=None, repr=False)

    def __post_init__(self):
        if (not self.bindings or not callable(self.cutover_authority)
                or any(not callable(getattr(obj, method, None)) for obj, method in (
                    (self.catalog, "for_plan"), (self.catalog, "for_admission"),
                    (self.planner, "binding"), (self.planner, "factory"),
                    (self.executor, "submit"), (self.executor, "read")))):
            raise ValueError("trusted_host_ports_required")


def _tasks(catalog):
    """The task ids registered for a plan in the T15 catalog (<root>/<plan_id>/<task_id>.json)."""
    root = Path(getattr(catalog, "root", "/nonexistent"))
    return lambda plan_id: sorted(p.stem for p in (root / plan_id).glob("*.json"))


@dataclass(frozen=True)
class TrustedRehearsalHost:
    app: object = field(repr=False)
    intake: ChangeIntake = field(repr=False)
    rehearsals: object = field(repr=False)
    evidence: object = field(repr=False)
    access: OperatorAccess = field(repr=False)


def build_trusted_host(store, *, ports: TrustedHostPorts, records, sender, verifier,
                       token_supplier, clock=None) -> TrustedRehearsalHost:
    """The same runner-owned signed store supplies delivery AND original reads.

    Construction does not execute a rehearsal. Human permission/admission comes
    first; T15's existing sender claims once. An unknown delivery is never retried
    here. Host crash semantics and T15's retry limits remain unchanged.
    """
    if type(ports) is not TrustedHostPorts or not isinstance(verifier, OperatorTokenVerifier) or not callable(token_supplier):
        raise ValueError("trusted_host_inputs_required")
    access = OperatorAccess(store, clock=clock)

    def deliver(submission):
        return submit_candidate(submission, candidate_type=Candidate, intake=intake,
                                token_supplier=token_supplier)

    dispatcher = AttemptDispatcher()
    rehearsals, evidence = build_bucket_host(ports.catalog, ports.planner, records,
                                             deliver=deliver, dispatch=dispatcher)
    intake = ChangeIntake(store, sender=sender, read_original=evidence.read_original,
                          authorize_hold=OperatorHoldPolicy(access, verifier), clock=clock)
    app = create_operator_cutover_app(store, ports.executor, bindings=ports.bindings,
        verifier=verifier, authority=ports.cutover_authority, rehearsal_host=rehearsals,
        change_authority_wrapper=ports.change_authority_wrapper,
        **({"failback_authority": ports.failback_authority, "read_integrity": ports.read_integrity,
            "edge": ports.edge} if ports.failback_authority is not None else {}),
        change_intake=intake, clock=clock, delegation=ports.delegation, list_tasks=_tasks(ports.catalog),
        delegated_shift=ports.delegated_shift, first_run=ports.first_run,
        **({"iac_repository": ports.iac} if ports.iac is not None else {}))
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                yield
            finally:
                await asyncio.to_thread(dispatcher.close)

    app.router.lifespan_context = lifespan
    return TrustedRehearsalHost(app, intake, rehearsals, evidence, access)
