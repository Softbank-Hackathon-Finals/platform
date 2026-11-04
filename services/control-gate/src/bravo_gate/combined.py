"""Join authenticated T14 security and T13 preparation records for T10.

The host supplies current records under its plan lock. This is not a replacement
for T10 approval/SHIFT/lease checks, nor for T12's final stopped-write comparison.
"""

from dataclasses import dataclass
from datetime import timedelta

from .contract import GateError, decode, fingerprint, invoke, time
from .models import RunSnapshot
from .readiness import ReadinessBinding, assess_readiness
from .service import GateService, accept


@dataclass(frozen=True)
class GatePrerequisites:
    context_digest: str
    report_digest: str
    readiness_binding_digest: str
    valid_until: str
    exception_ids: tuple[str, ...]

    @property
    def authorizes_cutover(self):
        return False


class CombinedGateService:
    def __init__(self, *, security: GateService, binding_provider, snapshot_provider,
                 authenticate_binding, authenticate_snapshot):
        if not isinstance(security, GateService) or not all(callable(c) for c in (
                binding_provider, snapshot_provider, authenticate_binding, authenticate_snapshot)):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.security = security
        self.binding_provider = binding_provider
        self.snapshot_provider = snapshot_provider
        self.authenticate_binding = authenticate_binding
        self.authenticate_snapshot = authenticate_snapshot

    def _load(self, actor, context_id, approval_ids, action, deadlines):
        # Reuses tenant-scoped authentication and current actor permissions;
        # the browser never chooses a policy, scope or evidence document here.
        ctx, *_ = self.security._load(actor, context_id, approval_ids,
                                      action=action, deadlines=deadlines)
        binding = invoke(self.binding_provider, actor, ctx)
        if not isinstance(binding, ReadinessBinding) or binding.context_digest != ctx.fingerprint:
            raise GateError("SERVER_READINESS_BINDING_REQUIRED")
        accept(self.authenticate_binding, actor, ctx, binding,
               expected=(ctx.fingerprint, binding.fingerprint, "readiness-binding"),
               deadlines=deadlines, clock=self.security.clock)
        snapshot = invoke(self.snapshot_provider, actor, ctx, binding)
        if (not isinstance(snapshot, RunSnapshot) or snapshot.ref != binding.snapshot_ref
                or snapshot.fingerprint != binding.snapshot_digest):
            raise GateError("READINESS_BINDING_CHANGED")
        accept(self.authenticate_snapshot, actor, ctx, binding, snapshot,
               expected=(ctx.fingerprint, snapshot.fingerprint, "readiness-snapshot"),
               deadlines=deadlines, clock=self.security.clock)
        return ctx, binding, snapshot

    def _check_current(self, actor, context_id, approval_ids, loaded, action, deadlines):
        if self._load(actor, context_id, approval_ids, action, deadlines) != loaded:
            raise GateError("READINESS_AUTHORITY_CHANGED")

    def evaluate(self, actor, context_id, *, approval_ids=()):
        start = self.security.clock()
        deadlines = []
        loaded = self._load(actor, context_id, approval_ids, "gate:evaluate", deadlines)
        ctx, binding, snapshot = loaded
        security_report = self.security.evaluate(actor, context_id, approval_ids=approval_ids)
        if security_report["context_digest"] != ctx.fingerprint:
            raise GateError("GATE_AUTHORITY_CHANGED")
        at = self.security.clock()
        data_report = assess_readiness(ctx, binding, snapshot, now=at)
        self._check_current(actor, context_id, approval_ids, loaded, "gate:evaluate", deadlines)
        ended = self.security.clock()
        if ended < at or at < start:
            raise GateError("CLOCK_REGRESSION")
        if data_report != assess_readiness(ctx, binding, snapshot, now=ended):
            raise GateError("READINESS_EXPIRED_DURING_EVALUATION")
        if deadlines and min(deadlines) <= ended:
            raise GateError("AUTHORITY_BINDING_OR_EXPIRY")
        # A report is a historical result, not an authorization. Consumption
        # revalidates both sides with current records and gate:consume permission.
        return {"kind": "combined-gate.v1", "context_digest": ctx.fingerprint,
                "readiness_binding_digest": binding.fingerprint, "evaluated_at": at.isoformat(),
                "decision": "PASS" if security_report["decision"] == data_report["decision"] == "PASS" else "BLOCK",
                "security": security_report, "data": data_report}

    def require_prerequisites(self, actor, context_id, document, *, approval_ids=()):
        candidate = decode(document)
        if (set(candidate) != {"kind", "context_digest", "readiness_binding_digest", "evaluated_at",
                               "decision", "security", "data"}
                or candidate["kind"] != "combined-gate.v1" or candidate["decision"] != "PASS"):
            raise GateError("COMBINED_GATE_BLOCKED")
        at = self.security.clock()
        evaluated = time(candidate["evaluated_at"])
        if not evaluated <= at < evaluated + timedelta(seconds=60):
            raise GateError("COMBINED_GATE_EXPIRED")
        deadlines = []
        loaded = self._load(actor, context_id, approval_ids, "gate:consume", deadlines)
        ctx, binding, snapshot = loaded
        if (candidate["context_digest"] != ctx.fingerprint
                or candidate["readiness_binding_digest"] != binding.fingerprint):
            raise GateError("COMBINED_GATE_CONTEXT_MISMATCH")
        from .contract import canonical

        security = self.security.require_security(actor, context_id, canonical(candidate["security"]),
                                                 approval_ids=approval_ids)
        data = assess_readiness(ctx, binding, snapshot, now=self.security.clock())
        if data["decision"] != "PASS" or candidate["data"] != data:
            raise GateError("DATA_READINESS_BLOCKED_OR_CHANGED")
        self._check_current(actor, context_id, approval_ids, loaded, "gate:consume", deadlines)
        ended = self.security.clock()
        limits = deadlines + [time(security.valid_until), time(data["valid_until"]),
                              evaluated + timedelta(seconds=60), at + timedelta(seconds=5)]
        if (ended < at or ended >= min(limits) or security.context_digest != ctx.fingerprint):
            raise GateError("COMBINED_PREREQUISITES_EXPIRED")
        return GatePrerequisites(ctx.fingerprint, fingerprint(candidate), binding.fingerprint,
                                 min(limits).isoformat(), security.exception_ids)
