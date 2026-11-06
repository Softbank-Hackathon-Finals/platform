"""Optional T10 adapter. All providers run inside T10's existing plan transaction.

This module creates neither a state store nor a default authority. The trusted
host loads registered inputs using the supplied connection; HTTP input is never
a registration source. T10 retains ownership of permissions, SHIFT, holds,
approval persistence and the single dispatch claim.
"""

from dataclasses import dataclass, replace

from .combined import CombinedGateService
from .contract import GateError, canonical, decode, fingerprint, invoke, time
from .models import GateContext, RunSnapshot, identity
from .readiness import ReadinessBinding


@dataclass(frozen=True)
class PlanGateSession:
    service: CombinedGateService
    plan_id: str
    context_id: str
    context: GateContext
    report: RunSnapshot
    readiness: ReadinessBinding
    approval_ids: tuple[str, ...] = ()
    # The gate after a successful T31 application is checked against the plan's
    # recheck profile and bound to that application, never the initial gate.
    gate_phase: str = "initial"
    application_id: str | None = None

    def __post_init__(self):
        identity(self.plan_id)
        identity(self.context_id)
        if (not isinstance(self.service, CombinedGateService)
                or not isinstance(self.context, GateContext)
                or not isinstance(self.report, RunSnapshot)
                or not isinstance(self.readiness, ReadinessBinding)
                or self.readiness.context_digest != self.context.fingerprint
                or type(self.approval_ids) is not tuple
                or len(self.approval_ids) > 32
                or any(type(v) is not str for v in self.approval_ids)
                or len(set(self.approval_ids)) != len(self.approval_ids)
                or self.gate_phase not in ("initial", "post_apply")
                or (self.application_id is None) != (self.gate_phase == "initial")):
            raise GateError("INVALID_PLAN_GATE_SESSION")
        for value in self.approval_ids:
            identity(value)
        if self.application_id is not None:
            identity(self.application_id)

    @property
    def fingerprint(self):
        value = dict(plan_id=self.plan_id, context_id=self.context_id, context=self.context.fingerprint,
                     report_ref=self.report.ref, report=self.report.fingerprint,
                     readiness=self.readiness.fingerprint, approval_ids=self.approval_ids)
        if self.gate_phase != "initial":
            # Initial sessions keep their existing fingerprint, so pending approvals stay valid.
            value.update(gate_phase=self.gate_phase, application_id=self.application_id)
        return fingerprint(value)


class T10GateAuthority:
    """Compose T19 with a required current T10 operational authority.

    load_session(connection, principal, plan) must use that transaction and the
    authenticated tenant to read immutable records. Every revocation/registration
    writer must use the same plan lock. Arbitrary in-process Python remains a
    trusted boundary; this adapter cannot prove a callback's database isolation.
    """

    def __init__(self, *, upstream, load_session):
        if not callable(upstream) or not callable(load_session):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.upstream, self.load_session = upstream, load_session

    @staticmethod
    def _bind(session, principal, plan, current):
        if not isinstance(session, PlanGateSession):
            raise GateError("SERVER_PLAN_GATE_SESSION_REQUIRED")
        ctx = session.context
        spec = decode(plan["spec_json"].encode())
        prefix = "" if session.gate_phase == "initial" else "recheck_"
        if (session.plan_id != plan["plan_id"]
                or principal.tenant_id != plan["tenant_id"] or ctx.tenant_id != plan["tenant_id"]
                or spec["tenant_id"] != ctx.tenant_id or spec["plan_id"] != plan["plan_id"]
                or spec["workload_id"] != ctx.app or spec["plan_digest"] != ctx.plan_digest
                # T10 fixes the evaluation profile of each gate in the plan; a plan without one cannot bind.
                or spec.get(prefix + "profile_id") != ctx.profile_id
                or spec.get(prefix + "profile_version") != ctx.profile_version
                # The approval binding T10 stored names the same gate; absent means the initial gate.
                or current.binding.get("gate_phase", "initial") != session.gate_phase
                or current.binding.get("application_id") != session.application_id
                or current.binding.get("plan_id") != plan["plan_id"]
                or current.command.get("plan_id") != plan["plan_id"]
                or current.binding.get("plan_digest") != ctx.plan_digest
                or current.binding.get("gate_id") != ctx.gate_id
                or current.binding.get("context_digest") != ctx.fingerprint
                or current.binding.get("target_environment_id") != ctx.target_environment_id
                or session.report.ref != ctx.gate_id):
            raise GateError("PLAN_GATE_SCOPE_MISMATCH")
        for side in ("source", "target"):
            env = getattr(ctx, side + "_environment_id")
            endpoint = current.command.get(side, {})
            if (spec[side + "_environment_id"] != env or endpoint.get("environment_id") != env
                    or endpoint.get("connection_ref") != getattr(session.readiness, side + "_database_ref")):
                raise GateError("PLAN_GATE_DATABASE_PAIR_MISMATCH")

    def __call__(self, connection, principal, plan, *, now, approved_by, approval_id):
        # Optional dependency: importing bravo_gate alone never imports T10.
        from bravo_platform_events.cutover import CurrentCutover

        if connection is None or plan.get("state") != "SHIFT":
            raise GateError("PLAN_TRANSACTION_REQUIRED")
        # Detach upstream mutable objects so in-place provider changes cannot
        # rewrite what was checked earlier in this invocation.
        def current():
            value = self.upstream(connection, principal, plan, now=now,
                                  approved_by=approved_by, approval_id=approval_id)
            if not isinstance(value, CurrentCutover):
                raise GateError("OPERATIONAL_AUTHORITY_REQUIRED")
            identity(value.revision)
            return CurrentCutover(decode(canonical(value.binding)),
                                  decode(canonical(value.command)), value.revision)

        first = current()
        session = invoke(self.load_session, connection, principal, plan)
        self._bind(session, principal, plan, first)
        started = session.service.security.clock()
        if time(now) > started:
            raise GateError("CLOCK_REGRESSION")
        proof = session.service.require_prerequisites(
            principal.subject_id, session.context_id, session.report.raw,
            approval_ids=session.approval_ids)
        if (proof.context_digest != session.context.fingerprint
                or proof.readiness_binding_digest != session.readiness.fingerprint):
            raise GateError("PLAN_GATE_SCOPE_MISMATCH")
        latest = current()
        observed = invoke(self.load_session, connection, principal, plan)
        self._bind(observed, principal, plan, latest)
        ended = session.service.security.clock()
        if (latest != first or observed.fingerprint != session.fingerprint):
            raise GateError("PLAN_GATE_AUTHORITY_CHANGED")
        if ended < started or ended >= time(proof.valid_until):
            raise GateError("PLAN_GATE_PREREQUISITE_EXPIRED")
        # Do not put the moving five-second check deadline in persistent human
        # approval context: revalidation happens on every new admission instead.
        # Exact report/policy changes, however, invalidate that approval.
        revision = fingerprint(dict(kind="t19-t10-authority.v1", upstream=latest.revision,
                                    session=session.fingerprint,
                                    readiness=proof.readiness_binding_digest))
        return replace(latest, revision=revision)
