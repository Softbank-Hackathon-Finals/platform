"""Production composition of T19 for the workflow host and the operator API (GATE, SHIFT, cutover).

What T19 needs, from records the host already keeps under the plan lock:
- provenance: PlanAuthorityRecords — registered operator-issuer keys and published envelopes for
  context, run-source/run-target, readiness-binding/readiness-snapshot and gate-result. The host only
  verifies; no private key enters it. T13 subjects are read back from gate_readiness.
- permission: the plan's operator grants (OperatorAccess) for the gate actions, with their expiry.
- readiness: the newest T13 binding/snapshot recorded for the context (gate_readiness).
- exceptions: none are served. A request that names an exception finds nothing and stays BLOCK.

A missing record, signature or grant is never filled in: evaluation fails closed (BLOCK/forbidden).
"""

from datetime import UTC, datetime

from bravo_gate.authority_records import PlanAuthorityRecords
from bravo_gate.models import VerifiedAuthority, permission_subject
from bravo_gate.stored import StoredGateServices

from . import gate_readiness
from .executor import _timestamp
from .gate_records import GateRecords
from .store import EventError


def server_clock():
    return datetime.now(UTC)


def gate_records(store, access):
    """GateRecords whose gate actions (gate:record, gate:bind, gate:evaluate, ...) are operator grants."""
    def authorize(c, principal, plan, action):
        access.require(c, principal, plan, action)  # raises EventError("forbidden")
        return True

    return GateRecords(store, authorize=authorize)


def provenance(records, *, clock=server_clock):
    """Registered keys and published envelopes; T13 subjects come from the stored readiness records."""
    return PlanAuthorityRecords(records, clock=clock, external_subject_provider=gate_readiness.subject)


def service_factory(records, access, *, clock=server_clock):
    """StoredGateServices for GateRecords.evaluate, GateRecheck and the cutover/SHIFT authorities."""
    authority = provenance(records, clock=clock)

    def authorize(view, actor, context, action):
        try:
            grant = access._require_subject(view.c, view.plan, actor, action, clock().timestamp())
        except EventError:
            return None  # T19 reads anything but a scoped authority as not permitted
        return VerifiedAuthority(context.fingerprint, permission_subject(actor, context.fingerprint, action),
                                 "permission:" + action, _timestamp(grant["expires_at"]))

    return StoredGateServices(
        authority_provider=authority.authority,
        authorize=authorize,
        approval_provider=lambda *_: None,
        binding_provider=gate_readiness.binding_provider,
        snapshot_provider=gate_readiness.snapshot_provider,
        clock=clock,
    )


def load_gate(records, access, *, clock=server_clock):
    """`load_gate(view)` for ApplicationEvidence and the cutover/SHIFT authorities:
    (CombinedGateService, ReadinessBinding) from the same assembly; no readiness record → refused."""
    factory = service_factory(records, access, clock=clock)

    def load(view):
        readiness = gate_readiness.binding_provider(view, view.principal.subject_id, view.context)
        if readiness is None:
            raise EventError("gate_readiness_unavailable")
        return factory(view), readiness

    return load


def create(store, *, clock=server_clock):
    """`"gate": "bravo_platform_events.gate_services:create"` for the operator approval host
    (operator_ports): factory(store) -> (GateRecords, load_gate), both on the plan's operator grants."""
    from .operator_access import OperatorAccess

    access = OperatorAccess(store)
    records = gate_records(store, access)
    return records, load_gate(records, access, clock=clock)
