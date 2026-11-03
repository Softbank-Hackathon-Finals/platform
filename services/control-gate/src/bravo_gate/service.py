"""T10 boundary: current server context, authenticated runs and stored approvals.

No optional authentication callbacks, client-selected profile, arbitrary raw
approval array, LLM, resource execution or automatic exception creation.
"""

from datetime import timedelta

from .contract import GateError, decode, fingerprint, time
from .engine import consume, evaluate
from .models import GateContext, RunSnapshot, SecurityPrerequisite, StoredException, identity


def accept(callback, *args):
    result = callback(*args)
    if result is not None and result is not True:
        raise GateError("AUTHORITY_DENIED")


class GateService:
    def __init__(
        self,
        *,
        contract,
        context_provider,
        run_provider,
        approval_provider,
        authenticate_context,
        authenticate_run,
        authenticate_approval,
        authorize,
        clock,
        network_verifier=None,
    ):
        for callback in (
            context_provider,
            run_provider,
            approval_provider,
            authenticate_context,
            authenticate_run,
            authenticate_approval,
            authorize,
            clock,
        ):
            if not callable(callback):
                raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.contract = contract
        self.context_provider = context_provider
        self.run_provider = run_provider
        self.approval_provider = approval_provider
        self.authenticate_context = authenticate_context
        self.authenticate_run = authenticate_run
        self.authenticate_approval = authenticate_approval
        self.authorize = authorize
        self.clock = clock
        self.network_verifier = network_verifier

    def _load(self, actor, context_id, approval_ids):
        identity(actor)
        identity(context_id)
        ctx = self.context_provider(actor, context_id)
        if not isinstance(ctx, GateContext):
            raise GateError("SERVER_CONTEXT_REQUIRED")
        accept(self.authorize, actor, ctx, "gate:evaluate")
        accept(self.authenticate_context, actor, ctx)
        if (
            type(approval_ids) is not tuple
            or len(approval_ids) > 32
            or any(type(value) is not str or not value for value in approval_ids)
            or len(set(approval_ids)) != len(approval_ids)
        ):
            raise GateError("BOUNDED_UNIQUE_APPROVAL_REFERENCES_REQUIRED")
        for approval_id in approval_ids:
            identity(approval_id)
        snapshots = []
        for side in ("source", "target"):
            value = self.run_provider(actor, ctx, side)
            if (
                not isinstance(value, RunSnapshot)
                or value.ref != getattr(ctx, side + "_ref")
                or value.fingerprint != getattr(ctx, side + "_digest")
            ):
                raise GateError("RUN_CONTEXT_BINDING_CHANGED")
            accept(self.authenticate_run, actor, ctx, side, value)
            run = value.document
            self.contract.validate(run, "control_run")
            if (
                run["environment"]
                != {
                    "id": getattr(ctx, side + "_environment_id"),
                    "ref": getattr(ctx, side + "_environment_ref"),
                    "role": side,
                }
                or run["app"] != ctx.app
            ):
                raise GateError("RUN_RESOURCE_SCOPE_MISMATCH")
            snapshots.append(value)
        approvals = []
        for approval_id in approval_ids:
            accept(self.authorize, actor, ctx, "gate:use-exception")
            value = self.approval_provider(actor, ctx, approval_id)
            if (
                not isinstance(value, StoredException)
                or value.body.get("approval_id") != approval_id
            ):
                raise GateError("SERVER_APPROVAL_NOT_FOUND")
            accept(self.authorize, actor, ctx, "gate:use-exception")
            accept(self.authenticate_approval, actor, ctx, value)
            approvals.append(value)
        return ctx, tuple(snapshots), tuple(approvals)

    def _same(self, actor, context_id, approval_ids, loaded):
        if self._load(actor, context_id, approval_ids) != loaded:
            raise GateError("GATE_AUTHORITY_CHANGED")

    def _network(self, actor, ctx, deadlines):
        def check(side, test_id, row):
            from .network import VerifiedNetworkProof

            if self.network_verifier is None:
                return False
            try:
                result = self.network_verifier(actor, ctx, side, test_id, row)
            except GateError:
                return False
            if (
                not isinstance(result, VerifiedNetworkProof)
                or result.context_digest != ctx.fingerprint
                or result.side != side
                or result.test_id != test_id
                or result.output_digest != row["evidence"]["output_sha256"]
                or result.valid_until <= self.clock()
            ):
                return False
            deadlines.append(result.valid_until)
            return True

        return check

    def evaluate(self, actor, context_id, *, approval_ids=()):
        loaded = self._load(actor, context_id, approval_ids)
        ctx, runs, approvals = loaded
        at = self.clock()
        deadlines = []
        network = self._network(actor, ctx, deadlines)
        report = evaluate(
            self.contract,
            ctx,
            *[r.document for r in runs],
            now=at,
            approvals=approvals,
            network_verified=network,
        )
        self._same(actor, context_id, approval_ids, loaded)
        # A slow authority lookup cannot extend evidence/exception validity.
        final = self.clock()
        if final < at:
            raise GateError("CLOCK_REGRESSION")
        current = evaluate(
            self.contract,
            ctx,
            *[r.document for r in runs],
            now=final,
            approvals=approvals,
            network_verified=network,
        )
        if {k: v for k, v in report.items() if k != "evaluated_at"} != {
            k: v for k, v in current.items() if k != "evaluated_at"
        }:
            raise GateError("GATE_EXPIRED_DURING_EVALUATION")
        self._same(actor, context_id, approval_ids, loaded)
        ended = self.clock()
        if ended < final:
            raise GateError("CLOCK_REGRESSION")
        if report["decision"] == "PASS":
            limits = (
                self._deadlines(ctx, runs, approvals) + deadlines + [at + timedelta(seconds=60)]
            )
            if ended >= min(limits):
                raise GateError("GATE_EXPIRED_DURING_EVALUATION")
        return report

    def _deadlines(self, ctx, runs, approvals):
        required = set(
            self.contract.profile(ctx.profile_id, ctx.profile_version)["required_test_ids"]
        )
        result = [
            time(row["evidence"]["expires_at"])
            for run in runs
            for row in run.document["results"]
            if row["test_id"] in required and row["evidence"] is not None
        ]
        result.extend(time(r.body["expires_at"]) for r in approvals)
        return result

    def require_security(self, actor, context_id, document, *, approval_ids=()):
        loaded = self._load(actor, context_id, approval_ids)
        ctx, runs, approvals = loaded
        candidate = decode(document)
        at = self.clock()
        deadlines = []
        network = self._network(actor, ctx, deadlines)
        report = consume(
            self.contract,
            ctx,
            candidate,
            *[r.document for r in runs],
            now=at,
            approvals=approvals,
            network_verified=network,
        )
        self._same(actor, context_id, approval_ids, loaded)
        final = self.clock()
        if final < at:
            raise GateError("CLOCK_REGRESSION")
        consume(
            self.contract,
            ctx,
            candidate,
            *[r.document for r in runs],
            now=final,
            approvals=approvals,
            network_verified=network,
        )
        deadlines.extend(
            [final + timedelta(seconds=5), time(candidate["evaluated_at"]) + timedelta(seconds=60)]
        )
        deadlines.extend(self._deadlines(ctx, runs, approvals))
        self._same(actor, context_id, approval_ids, loaded)
        ended = self.clock()
        if ended < final or min(deadlines) <= ended:
            raise GateError("SECURITY_PREREQUISITE_EXPIRED")
        return SecurityPrerequisite(
            ctx.fingerprint,
            fingerprint(report),
            min(deadlines).isoformat(),
            tuple(row["exception"]["approval_id"] for row in report["rows"] if row["exception"]),
        )
