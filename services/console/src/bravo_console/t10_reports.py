"""Read an authenticated T19 report through T10's current plan transaction.

Optional console[t10] host adapter. The host supplies verified request identity,
current GateRecords grants and T19 report provenance. No enrollment, default
identity/trust, SQL writes, approvals or execution are provided here.
"""

import asyncio
import inspect
import json
from datetime import UTC, datetime

from bravo_gate.contract import server_time
from bravo_gate.service import accept
from bravo_gate.signatures import Ed25519Authority
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal, authorized_plan

from .observations import ObservationError
from .reports import RegisteredGateDocument


class T10ControlReports:
    def __init__(self, records, *, resolve_principal, authenticate_report=None,
                 authority_factory=None, clock=None):
        if (
            not isinstance(records, GateRecords)
            or not callable(resolve_principal)
            or inspect.iscoroutinefunction(resolve_principal)
            or (authenticate_report is None) == (authority_factory is None)
            or any(c is not None and (not callable(c) or inspect.iscoroutinefunction(c))
                   for c in (authenticate_report, authority_factory))
            or (clock is not None and not callable(clock))
        ):
            raise ValueError("explicit_t10_report_authority_required")
        self.records = records
        self.resolve_principal = resolve_principal
        self.authenticate_report = authenticate_report
        self.authority_factory = authority_factory
        source_clock = clock or (lambda: datetime.now(UTC))
        self.clock = lambda: server_time(source_clock)

    def _read(self, scope, snapshot):
        principal = self.resolve_principal(scope)
        if not isinstance(principal, Principal):
            raise ObservationError("control_report_unverified")
        plan_id = snapshot["plan_id"]
        # write=True selects the existing per-plan advisory-lock protocol. This
        # adapter executes SELECTs only. All grant/context writers must use the
        # same lock, so revocation cannot race inside this read transaction.
        with self.records.store._transaction(write=True, plan_id=plan_id) as c:
            plan = authorized_plan(c, principal, plan_id, None)
            view = self.records.view(c, principal, plan)
            if self.records.store._snapshot(c, plan) != snapshot:
                raise ObservationError("control_report_plan_changed")
            report = view.report_provider(principal.subject_id, view.context)
            if report is None or not 1 <= len(report.raw) <= 262144:
                raise ObservationError("control_report_unavailable")
            document = report.document
            if (
                report.ref != view.context.gate_id
                or document.get("gate_id") != view.context.gate_id
                or document.get("context_digest") != view.context.fingerprint
            ):
                raise ObservationError("control_report_unverified")
            start = self.clock()
            deadlines = []
            authenticate = self.authenticate_report
            if self.authority_factory is not None:
                # Resolve issuer keys/envelopes inside this exact plan lock.
                # A second DB transaction could deadlock or observe another
                # registration revision. Never cache this connection-bound
                # authority or treat its mere construction as verification.
                authority = self.authority_factory(view)
                if not isinstance(authority, Ed25519Authority):
                    raise ObservationError("control_report_unverified")

                def authenticate(actor, context, candidate):
                    view._check(actor, context)
                    proof = authority.verify(context, candidate.fingerprint, "gate-result")
                    view._check(actor, context)
                    return proof

            # Reuse T19's exact provenance/expiry binding, including scoped
            # VerifiedAuthority. A database row or hash alone is never authority.
            accept(
                authenticate,
                principal.subject_id,
                view.context,
                report,
                expected=(view.context.fingerprint, report.fingerprint, "gate-result"),
                deadlines=deadlines,
                clock=self.clock,
            )
            if view.report_provider(principal.subject_id, view.context) != report:
                raise ObservationError("control_report_unverified")
            end = self.clock()
            if end < start or (deadlines and min(deadlines) <= end):
                raise ObservationError("control_report_unverified")
            return RegisteredGateDocument(
                plan_id,
                json.loads(plan["spec_json"])["plan_digest"],
                report.raw,
                view.gate_phase,
                view.application_id,
                min(deadlines) if deadlines else None,
            )

    async def load(self, scope, snapshot):
        try:
            return await asyncio.to_thread(self._read, scope, snapshot)
        except Exception:
            # Storage, issuer and resolver diagnostics may contain private data.
            raise ObservationError("control_report_unavailable") from None

    async def authenticate(self, scope, snapshot, candidate):
        if not isinstance(candidate, RegisteredGateDocument):
            return False
        try:
            return await asyncio.to_thread(self._read, scope, snapshot) == candidate
        except Exception:
            return False
