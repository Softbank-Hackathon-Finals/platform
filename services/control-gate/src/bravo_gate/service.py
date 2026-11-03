"""T10 boundary: current server context, authenticated runs and stored approvals.

No optional authentication callbacks, client-selected profile, arbitrary raw
approval array, LLM, resource execution or automatic exception creation.
"""

from datetime import timedelta

from .contract import GateError, decode, fingerprint, invoke, server_time, time
from .engine import consume, evaluate
from .models import (
    GateContext,
    RunSnapshot,
    SecurityPrerequisite,
    StoredException,
    VerifiedAuthority,
    identity,
    permission_subject,
)


def accept(callback, *args, expected=None, deadlines=None, clock=None):
    result = invoke(callback, *args)
    if isinstance(result, VerifiedAuthority) and expected is not None:
        if (
            (result.context_digest, result.subject_digest, result.purpose) != expected
            or deadlines is None
            or clock is None
            or result.valid_until <= clock()
        ):
            raise GateError("AUTHORITY_BINDING_OR_EXPIRY")
        deadlines.append(result.valid_until)
    elif result is not True:
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
        self.clock = lambda: server_time(clock)
        self.network_verifier = network_verifier

    def _load(self, actor, context_id, approval_ids, *, action, deadlines):
        identity(actor)
        identity(context_id)
        ctx = invoke(self.context_provider, actor, context_id)
        if not isinstance(ctx, GateContext):
            raise GateError("SERVER_CONTEXT_REQUIRED")
        accept(
            self.authorize,
            actor,
            ctx,
            action,
            expected=(
                ctx.fingerprint,
                permission_subject(actor, ctx.fingerprint, action),
                "permission:" + action,
            ),
            deadlines=deadlines,
            clock=self.clock,
        )
        accept(
            self.authenticate_context,
            actor,
            ctx,
            expected=(ctx.fingerprint, ctx.fingerprint, "context"),
            deadlines=deadlines,
            clock=self.clock,
        )
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
            value = invoke(self.run_provider, actor, ctx, side)
            if (
                not isinstance(value, RunSnapshot)
                or value.ref != getattr(ctx, side + "_ref")
                or value.fingerprint != getattr(ctx, side + "_digest")
            ):
                raise GateError("RUN_CONTEXT_BINDING_CHANGED")
            accept(
                self.authenticate_run,
                actor,
                ctx,
                side,
                value,
                expected=(ctx.fingerprint, value.fingerprint, "run-" + side),
                deadlines=deadlines,
                clock=self.clock,
            )
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
            accept(
                self.authorize,
                actor,
                ctx,
                "gate:use-exception",
                expected=(
                    ctx.fingerprint,
                    permission_subject(actor, ctx.fingerprint, "gate:use-exception"),
                    "permission:gate:use-exception",
                ),
                deadlines=deadlines,
                clock=self.clock,
            )
            value = invoke(self.approval_provider, actor, ctx, approval_id)
            if (
                not isinstance(value, StoredException)
                or value.body.get("approval_id") != approval_id
            ):
                raise GateError("SERVER_APPROVAL_NOT_FOUND")
            accept(
                self.authorize,
                actor,
                ctx,
                "gate:use-exception",
                expected=(
                    ctx.fingerprint,
                    permission_subject(actor, ctx.fingerprint, "gate:use-exception"),
                    "permission:gate:use-exception",
                ),
                deadlines=deadlines,
                clock=self.clock,
            )
            accept(
                self.authenticate_approval,
                actor,
                ctx,
                value,
                expected=(ctx.fingerprint, value.fingerprint, "exception"),
                deadlines=deadlines,
                clock=self.clock,
            )
            approvals.append(value)
        return ctx, tuple(snapshots), tuple(approvals)

    def _same(self, actor, context_id, approval_ids, loaded, *, action, deadlines):
        if (
            self._load(actor, context_id, approval_ids, action=action, deadlines=deadlines)
            != loaded
        ):
            raise GateError("GATE_AUTHORITY_CHANGED")

    def _network(self, actor, ctx, deadlines):
        def check(side, test_id, row):
            from .network import VerifiedNetworkProof

            if self.network_verifier is None:
                return False
            try:
                result = invoke(self.network_verifier, actor, ctx, side, test_id, row)
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
        action = "gate:evaluate"
        deadlines = []
        loaded = self._load(actor, context_id, approval_ids, action=action, deadlines=deadlines)
        ctx, runs, approvals = loaded
        at = self.clock()
        network = self._network(actor, ctx, deadlines)
        report = evaluate(
            self.contract,
            ctx,
            *[r.document for r in runs],
            now=at,
            approvals=approvals,
            network_verified=network,
        )
        self._same(actor, context_id, approval_ids, loaded, action=action, deadlines=deadlines)
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
        self._same(actor, context_id, approval_ids, loaded, action=action, deadlines=deadlines)
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
        result.extend(time(r.body.get("expires_at")) for r in approvals)
        return result

    def require_security(self, actor, context_id, document, *, approval_ids=()):
        action = "gate:consume"
        deadlines = []
        loaded = self._load(actor, context_id, approval_ids, action=action, deadlines=deadlines)
        ctx, runs, approvals = loaded
        candidate = decode(document)
        at = self.clock()
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
        self._same(actor, context_id, approval_ids, loaded, action=action, deadlines=deadlines)
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
        self._same(actor, context_id, approval_ids, loaded, action=action, deadlines=deadlines)
        ended = self.clock()
        if ended < final or min(deadlines) <= ended:
            raise GateError("SECURITY_PREREQUISITE_EXPIRED")
        return SecurityPrerequisite(
            ctx.fingerprint,
            fingerprint(candidate),
            min(deadlines).isoformat(),
            tuple(row["exception"]["approval_id"] for row in report["rows"] if row["exception"]),
        )
