"""Compose existing T19 services with T10's locked, current record view.

This optional host adapter provides wiring, not an issuer or an intake server.
The host must supply current provenance, grants, exception and T13 stores. It
never signs an uploaded document, invents readiness or supplies a default grant.
"""

from .combined import CombinedGateService
from .contract import GateError, T5Contract, invoke
from .service import GateService
from .signatures import Ed25519Authority


class StoredGateServices:
    """A service_factory for GateRecords.evaluate and cutover_authority.

    All supplied providers take the connection-bound GateRecordView first and
    must use its plan transaction. The Ed25519 authority's key/envelope readers
    must do the same. No view or constructed service may outlive that transaction.
    Actual producer qualification and issuer enrollment remain host duties.
    """

    def __init__(self, *, authority_provider, authorize, approval_provider,
                 binding_provider, snapshot_provider, clock, network_verifier=None):
        if not all(callable(v) for v in (
            authority_provider, authorize, approval_provider, binding_provider,
            snapshot_provider, clock,
        )) or (network_verifier is not None and not callable(network_verifier)):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.authority_provider = authority_provider
        self.authorize = authorize
        self.approval_provider = approval_provider
        self.binding_provider = binding_provider
        self.snapshot_provider = snapshot_provider
        self.clock = clock
        self.network_verifier = network_verifier
        self.contract = T5Contract()

    def __call__(self, view):
        # Optional dependency: the base evaluator does not import T10.
        from bravo_platform_events.gate_records import GateRecordView

        if not isinstance(view, GateRecordView):
            raise GateError("SERVER_RECORD_VIEW_REQUIRED")
        view._check(view.principal.subject_id, view.context)
        authority = invoke(self.authority_provider, view)
        if not isinstance(authority, Ed25519Authority):
            raise GateError("REGISTERED_SIGNATURE_AUTHORITY_REQUIRED")

        def checked(actor, context):
            view._check(actor, context)

        def provenance(actor, context, subject, purpose):
            checked(actor, context)
            # A platform re-evaluation (SHIFT/cutover only, same transaction) is its own provenance:
            # its inputs already passed their signatures and T19 computed it here (sangmu1126 #565).
            served = getattr(view, "reevaluation_authority", None)
            proof = served(context, subject) if purpose == "gate-result" and callable(served) else None
            if proof is not None:
                checked(actor, context)
                return proof
            proof = authority.verify(context, subject, purpose)
            checked(actor, context)
            return proof

        def authorize(actor, context, action):
            checked(actor, context)
            proof = invoke(self.authorize, view, actor, context, action)
            checked(actor, context)
            # Return the original scoped authority including its deadline.
            # GateService.accept validates it, or accepts exact True only for
            # an explicitly timeless, live server policy.
            return proof

        def approval(actor, context, approval_id):
            checked(actor, context)
            value = invoke(self.approval_provider, view, actor, context, approval_id)
            checked(actor, context)
            return value

        def binding(actor, context):
            checked(actor, context)
            value = invoke(self.binding_provider, view, actor, context)
            checked(actor, context)
            return value

        def snapshot(actor, context, registered):
            checked(actor, context)
            value = invoke(self.snapshot_provider, view, actor, context, registered)
            checked(actor, context)
            return value

        def network(actor, context, side, test_id, row):
            checked(actor, context)
            value = invoke(self.network_verifier, view, actor, context, side, test_id, row)
            checked(actor, context)
            return value

        security = GateService(
            contract=self.contract,
            context_provider=view.context_provider,
            run_provider=view.run_provider,
            report_provider=view.report_provider,
            approval_provider=approval,
            authenticate_context=lambda actor, ctx: provenance(
                actor, ctx, ctx.fingerprint, "context"),
            authenticate_run=lambda actor, ctx, side, run: provenance(
                actor, ctx, run.fingerprint, "run-" + side),
            authenticate_report=lambda actor, ctx, report: provenance(
                actor, ctx, report.fingerprint, "gate-result"),
            authenticate_approval=lambda actor, ctx, value: provenance(
                actor, ctx, value.fingerprint, "exception"),
            authorize=authorize,
            clock=self.clock,
            network_verifier=network if self.network_verifier is not None else None,
        )
        return CombinedGateService(
            security=security,
            binding_provider=binding,
            snapshot_provider=snapshot,
            authenticate_binding=lambda actor, ctx, value: provenance(
                actor, ctx, value.fingerprint, "readiness-binding"),
            authenticate_snapshot=lambda actor, ctx, registered, value: provenance(
                actor, ctx, value.fingerprint, "readiness-snapshot"),
        )
