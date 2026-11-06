"""Current T10 operator grants and T19 originals for one registered SHIFT run.

All records share T10's existing plan lock. Registration binds an already
approved workflow checkpoint; it cannot create a SHIFT state or approval.
"""

import json
from dataclasses import asdict

from bravo_gate.contract import canonical
from bravo_gate.t10 import PlanGateSession
from bravo_platform_events.operator_access import OperatorAccess

from .host import CurrentShift, _digest, _get, _key, _put, _time
from .provider import Binding, EdgeError


class ShiftAuthority:
    def __init__(self, store, *, run_id, load_session, clock):
        if not callable(load_session) or not callable(clock):
            raise ValueError("current_gate_session_required")
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise ValueError("invalid_shift_registration")
        self.run_id = run_id
        self.store, self.load_session, self.clock = store, load_session, clock
        self.access = OperatorAccess(store, clock=clock)

    def _checkpoint(self, c, plan):
        if plan["state"] != "SHIFT":
            raise EdgeError("current_shift_required")
        saved = _get(c, plan["plan_id"], f"workflow:{plan['version'] - 1}")
        if saved is None:
            raise EdgeError("shift_checkpoint_required")
        proof = saved[1]
        spec = json.loads(plan["spec_json"])
        approval = proof.get("result", {})
        if (proof.get("plan_id") != plan["plan_id"]
                or proof.get("version") != plan["version"] - 1
                or proof.get("plan_digest") != spec["plan_digest"]
                or proof.get("target_environment_id") != spec["target_environment_id"]
                or approval.get("approved") is not True
                or not isinstance(approval.get("approval_id"), str)
                or _time(approval["expires_at"]) <= _time(self.clock())):
            raise EdgeError("shift_approval_unavailable")
        return proof

    def _gate(self, c, principal, plan, proof):
        session = self.load_session(c, principal, plan)
        if not isinstance(session, PlanGateSession):
            raise EdgeError("current_gate_session_required")
        ctx, gate = session.context, proof["result"]["gate"]
        spec = json.loads(plan["spec_json"])
        if (session.plan_id != plan["plan_id"] or ctx.tenant_id != plan["tenant_id"]
                or ctx.app != spec["workload_id"] or ctx.plan_digest != spec["plan_digest"]
                or ctx.profile_id != spec.get("recheck_profile_id", spec.get("profile_id"))
                or ctx.profile_version != spec.get("recheck_profile_version", spec.get("profile_version"))
                or ctx.source_environment_id != spec["source_environment_id"]
                or ctx.target_environment_id != spec["target_environment_id"]
                or session.report.ref != ctx.gate_id or gate.get("verdict") != "PASS"
                or gate.get("gate_id") != ctx.gate_id
                or gate.get("context_digest") != ctx.fingerprint):
            raise EdgeError("shift_gate_binding_mismatch")
        # The stored GATE report is only valid for 60 s and AWAIT_APPROVAL can take hours (#532,
        # sangmu1126 15:21Z): evaluate again with current records right before SHIFT and consume
        # that report only if it is PASS and bound to the same context and readiness as the GATE
        # report. Otherwise stop; no approval is revived or re-requested here.
        stored = json.loads(session.report.raw)
        fresh = session.service.evaluate(principal.subject_id, session.context_id,
                                         approval_ids=session.approval_ids)
        if (fresh.get("decision") != "PASS" or fresh.get("context_digest") != ctx.fingerprint
                or fresh.get("readiness_binding_digest") != stored.get("readiness_binding_digest")):
            raise EdgeError("shift_gate_reevaluation_blocked")
        # A separate record next to (never over) the GATE report, so both judgements stay auditable.
        _put(c, plan["plan_id"], _key("shift-gate-reevaluation", [self.run_id, fresh["evaluated_at"]]),
             {"run_id": self.run_id, "gate_report_ref": session.report.ref}, fresh)
        from bravo_platform_events.gate_records import record_reevaluation

        record_reevaluation(c, plan["plan_id"], ctx.gate_id, fresh)  # T19 consumes only the registered result
        verified = session.service.require_prerequisites(
            principal.subject_id, session.context_id, canonical(fresh), approval_ids=session.approval_ids)
        latest = self.load_session(c, principal, plan)
        if (not isinstance(latest, PlanGateSession)
                or latest.fingerprint != session.fingerprint
                or verified.context_digest != ctx.fingerprint
                or verified.readiness_binding_digest != session.readiness.fingerprint
                or _time(verified.valid_until) <= _time(self.clock())):
            raise EdgeError("shift_gate_changed_or_expired")
        return session.fingerprint

    def register(self, principal, plan_id, *, run_id, profile, binding):
        """Trusted host command after a human's existing SHIFT approval.

        The host selects the registered LB and T36 policy; these are not HTTP
        body fields. Exact retries read the same record and never reactivate a
        revoked run. An old plan/run is never relabelled as a new execution.
        """
        if not isinstance(binding, Binding) or not isinstance(run_id, str) or not 1 <= len(run_id) <= 256:
            raise ValueError("invalid_shift_registration")
        if run_id != self.run_id:
            raise EdgeError("registered_run_mismatch")
        from bravo_observability.sli import judge

        judge(profile, None, plan_id=plan_id, state="SHIFT", weight_target=0,
              observe_from=self.clock(), decided_at=self.clock(), prior_unknown=0)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise EdgeError("current_shift_required")
            plan = dict(row)
            grant = self.access.require(c, principal, plan, "approve_shift")
            proof = self._checkpoint(c, plan)
            gate_revision = self._gate(c, principal, plan, proof)
            value = dict(run_id=run_id, approval_id=proof["result"]["approval_id"],
                approved_by=principal.subject_id, approver_revision=grant["revision"],
                plan_version=plan["version"],
                checkpoint_digest=_digest(proof), gate_revision=gate_revision,
                profile=profile, lb_binding=asdict(binding))
            self.access.require(c, principal, plan, "approve_shift")
            self._checkpoint(c, plan)
            key = _key("registered-run", [proof["result"]["approval_id"], run_id])
            _put(c, plan_id, key, {}, value)
            return value

    def revoke(self, principal, plan_id, *, approval_id):
        """Append a revocation under the same plan lock; never clears a journal."""
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise EdgeError("current_shift_required")
            self.access.require(c, principal, dict(row), "approve_shift")
            if _get(c, plan_id, _key("registered-run", [approval_id, self.run_id])) is None:
                raise EdgeError("shift_registration_required")
            _put(c, plan_id, _key("revoked-run", approval_id), {}, dict(revoked=True))

    def __call__(self, c, principal, plan):
        grant = self.access.require(c, principal, plan, "execute_shift")
        proof = self._checkpoint(c, plan)
        approval_id = proof["result"]["approval_id"]
        registered = _get(c, plan["plan_id"], _key("registered-run", [approval_id, self.run_id]))
        if registered is None or _get(c, plan["plan_id"], _key("revoked-run", approval_id)):
            raise EdgeError("shift_registration_unavailable")
        value = registered[1]
        if (value["plan_version"] != plan["version"]
                or value["checkpoint_digest"] != _digest(proof)):
            raise EdgeError("shift_registration_changed")
        approver = self.access._require_subject(c, plan, value["approved_by"], "approve_shift",
                                               _time(self.clock()).timestamp())
        if approver["revision"] != value["approver_revision"]:
            raise EdgeError("shift_approver_grant_changed")
        gate_revision = self._gate(c, principal, plan, proof)
        if gate_revision != value["gate_revision"]:
            raise EdgeError("shift_gate_changed_or_expired")
        self.access.require(c, principal, plan, "execute_shift")
        self.access._require_subject(c, plan, value["approved_by"], "approve_shift",
                                     _time(self.clock()).timestamp())
        self._checkpoint(c, plan)
        revision = _digest(dict(registration=value, executor=grant, approver=approver))
        return CurrentShift(approval_id, value["run_id"], revision,
                            value["profile"], value["lb_binding"])
