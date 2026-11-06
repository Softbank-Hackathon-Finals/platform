"""T19 records in T10's existing plan transaction, with no default authority.

The trusted host authenticates producers before this port. Persisting a record
does not authenticate its evidence: T19 still requires its current provenance,
permission, expiry and readiness providers at evaluation and consumption.
"""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import timedelta

from bravo_gate.combined import CombinedGateService
from bravo_gate.contract import T5Contract, canonical, time
from bravo_gate.models import GateContext, RunSnapshot, VerifiedAuthority, identity
from bravo_gate.t10 import PlanGateSession, T10GateAuthority

from .cutover import CurrentCutover
from .plan_api import authorized_plan
from .plans import _canonical
from .store import EventError


def _key(kind, ref):
    return "gate-" + kind + "-" + hashlib.sha256(ref.encode()).hexdigest()


def _get(c, plan_id, kind, ref):
    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key=?",
                    (plan_id, _key(kind, ref))).fetchone()
    return json.loads(row["receipt_json"]) if row else None


def _put(c, plan_id, kind, ref, value):
    old = _get(c, plan_id, kind, ref)
    if old is not None:
        if canonical(old) != canonical(value):
            raise EventError("gate_record_conflict")
        return
    c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
              (plan_id, _key(kind, ref), "{}", _canonical(value)))


_SERVED = ContextVar("bravo_gate_reevaluation", default=None)


def _readiness_scope(service, actor, context):
    """What a re-evaluation must keep from GATE, and when each readiness component was observed."""
    binding = service.binding_provider(actor, context)
    snapshot = service.snapshot_provider(actor, context, binding)
    # The same binding check as CombinedGateService._load: judge the snapshot that was evaluated.
    if snapshot.ref != binding.snapshot_ref or snapshot.fingerprint != binding.snapshot_digest:
        raise EventError("gate_reevaluation_scope_changed")
    doc = snapshot.document
    observed = {side: doc[side]["observed_at"] for side in ("source", "target", "replication")}
    observed |= {"sequence:" + row["table"] + "." + row["column"]: row["observed_at"] for row in doc.get("sequences", [])}
    return {"binding": binding.fingerprint, "policy": binding.policy.fingerprint,
            "source_database_ref": binding.source_database_ref, "target_database_ref": binding.target_database_ref,
            "snapshot_ref": binding.snapshot_ref, "snapshot_digest": binding.snapshot_digest, "observed": observed}


@contextmanager
def served_reevaluation(c, plan_id, session, principal):
    """SHIFT/cutover re-evaluation under the caller's plan lock (sangmu1126 #565 ①②).

    Passes only if the fresh combined report is PASS for the same context, the readiness binding
    keeps GATE's policy and database pair, and its snapshot is not older than GATE's (snapshot
    ref/digest may change and are recorded). Inside the block, and only for this transaction, the
    fresh security report is the registered result with platform provenance; everything else keeps
    seeing the signed GATE report.
    """
    ctx = session.context
    fresh = session.service.evaluate(principal.subject_id, session.context_id, approval_ids=session.approval_ids)
    if fresh.get("decision") != "PASS" or fresh.get("context_digest") != ctx.fingerprint:
        raise EventError("gate_reevaluation_blocked")
    now = _readiness_scope(session.service, principal.subject_id, ctx)
    gate = _get(c, plan_id, "readiness-scope", ctx.gate_id)
    if gate is None:
        # GATE recorded without its scope (earlier GATE or another composition): the GATE report's
        # binding digest still decides. The same binding means the same policy, DB pair and snapshot.
        if session.report.document.get("readiness_binding_digest") != now["binding"]:
            raise EventError("gate_readiness_scope_unavailable")
        gate = now
    if (any(now[key] != gate[key] for key in ("policy", "source_database_ref", "target_database_ref"))
            or fresh.get("readiness_binding_digest") != now["binding"]):
        raise EventError("gate_reevaluation_scope_changed")
    # Every component (source, target, replication, each sequence) must be at least as new as at
    # GATE; one component moving back is an older observation (sangmu1126 #565 P2).
    if (set(now["observed"]) != set(gate["observed"])
            or any(time(now["observed"][key]) < time(gate["observed"][key]) for key in gate["observed"])):
        raise EventError("gate_reevaluation_snapshot_older")
    record = {"kind": "platform-reevaluation.v1", "gate_report_ref": ctx.gate_id,
              "inputs": {"context": ctx.fingerprint, "runs": [ctx.source_digest, ctx.target_digest],
                         "exceptions": list(session.approval_ids), "binding": now["binding"]},
              "snapshot": {"ref": now["snapshot_ref"], "digest": now["snapshot_digest"]}, "report": fresh}
    key = ("gate-reevaluated-" + hashlib.sha256(ctx.gate_id.encode()).hexdigest()[:32] + "-"
           + fresh["evaluated_at"] + "-" + hashlib.sha256(_canonical(record).encode()).hexdigest()[:16])
    if c.execute("SELECT 1 FROM plan_commands WHERE plan_id=? AND command_key=?", (plan_id, key)).fetchone() is None:
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (plan_id, key, "{}", _canonical(record)))
    token = _SERVED.set((c, plan_id, ctx.gate_id, fresh))
    try:
        yield fresh
    finally:
        _SERVED.reset(token)


def _current(c, plan_id):
    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? "
                    "AND command_key LIKE ? ORDER BY command_key DESC LIMIT 1",
                    (plan_id, "gate-current-%")).fetchone()
    return json.loads(row["receipt_json"]) if row else None


def _application_checkpoint(c, plan):
    """Read the successful workflow Application, not a pre-effect admission claim."""
    spec = json.loads(plan["spec_json"])
    found = []
    for row in c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? "
                         "AND command_key LIKE ?", (plan["plan_id"], "workflow:%")).fetchall():
        proof = json.loads(row["receipt_json"])
        result = proof["result"]
        if "application_id" not in result:
            continue
        if (proof["plan_id"] != plan["plan_id"] or proof["plan_digest"] != spec["plan_digest"]
                or proof["target_environment_id"] != spec["target_environment_id"]
                or not 0 < proof["version"] < plan["version"]
                or result.get("job_passed") is not True
                or result.get("deployed_digest") != result["change"]["image_digest"]):
            raise EventError("application_checkpoint_mismatch")
        found.append((proof["version"], result["application_id"]))
    return max(found)[1] if found else None


def _profile_matches(c, plan, context, gate_phase, application_id):
    spec = json.loads(plan["spec_json"])
    applied = _application_checkpoint(c, plan)
    if gate_phase == "initial":
        if application_id is not None or applied is not None:
            return False
        prefix = ""
    elif gate_phase == "post_apply":
        if application_id is None or application_id != applied:
            return False
        prefix = "recheck_"
    else:
        return False
    return (spec.get(prefix + "profile_id") == context.profile_id
            and spec.get(prefix + "profile_version") == context.profile_version)


class GateRecords:
    """Trusted host port. No HTTP enrollment, new tables or separate plan lock.

    authorize(connection, principal, plan, action) must check current host grants
    and return exactly True. Actions are gate:record, gate:bind, gate:evaluate,
    gate:read. A missing policy fails closed; read permission alone cannot write.
    Every grant/binding writer must follow the store's existing plan lock.
    """

    def __init__(self, store, *, authorize):
        if not callable(authorize):
            raise ValueError("gate_authorization_required")
        self.store, self.authorize = store, authorize
        self.contract = T5Contract()

    def _plan(self, c, principal, plan_id, action):
        plan = authorized_plan(c, principal, plan_id, None)
        if self.authorize(c, principal, plan, action) is not True:
            raise EventError("forbidden")
        return plan

    def cutover_authority(self, *, upstream, load_gate):
        """Compose stored phase metadata and T19 checks under the existing plan lock.

        The host still supplies current operational authority and authenticated
        gate/readiness providers. Neither persisted bytes nor this adapter grant
        permission, manufacture a passing gate, or execute a cutover.
        """
        if not callable(upstream) or not callable(load_gate):
            raise ValueError("gate_cutover_providers_required")

        def current(connection, principal, plan, **kwargs):
            value = upstream(connection, principal, plan, **kwargs)
            if not isinstance(value, CurrentCutover):
                raise EventError("authority_unavailable")
            view = self.view(connection, principal, plan)
            return replace(value, binding=view.cutover_binding(value.binding))

        def session(connection, principal, plan):
            view = self.view(connection, principal, plan)
            service, readiness = load_gate(view)
            return view.session(service, readiness)

        return T10GateAuthority(upstream=current, load_session=session)

    def record_run(self, principal, plan_id, side, snapshot):
        """Stage an authenticated producer's original, including UNKNOWN results.

        One side may arrive first. It cannot create a context or a gate result
        until the other registered side exists. Never copy source into target.
        """
        if side not in {"source", "target"} or not isinstance(snapshot, RunSnapshot):
            raise ValueError("invalid_gate_run")
        doc = snapshot.document
        self.contract.validate(doc, "control_run")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = self._plan(c, principal, plan_id, "gate:record")
            spec = json.loads(plan["spec_json"])
            if (doc["app"] != spec["workload_id"] or doc["environment"]["role"] != side
                    or doc["environment"]["id"] != spec[side + "_environment_id"]):
                raise EventError("gate_run_scope_mismatch")
            _put(c, plan_id, "run", snapshot.ref,
                 dict(side=side, document=doc, digest=snapshot.fingerprint))
        return dict(ref=snapshot.ref, digest=snapshot.fingerprint)

    def read_run(self, connection, principal, plan, ref):
        """Read one staged original even while the other environment is missing."""
        identity(ref)
        current = self._plan(connection, principal, plan["plan_id"], "gate:read")
        if current != dict(plan):
            raise EventError("gate_plan_changed")
        record = _get(connection, plan["plan_id"], "run", ref)
        if record is None:
            raise EventError("gate_run_unavailable_or_changed")
        snapshot = RunSnapshot(ref, canonical(record["document"]))
        if snapshot.fingerprint != record["digest"]:
            raise EventError("gate_run_unavailable_or_changed")
        return snapshot

    def recheck_profile(self, principal, plan_id, application_id):
        """Select the immutable post-apply profile before invoking collectors."""
        identity(application_id)
        with self.store._transaction() as c:
            plan = self._plan(c, principal, plan_id, "gate:bind")
            if plan["state"] != "GATE":
                raise EventError("gate_stage_required")
            if _application_checkpoint(c, plan) != application_id:
                raise EventError("application_checkpoint_mismatch")
            spec = json.loads(plan["spec_json"])
            if not spec.get("recheck_profile_id") or not spec.get("recheck_profile_version"):
                raise EventError("gate_recheck_profile_required")
            return dict(profile_id=spec["recheck_profile_id"],
                        profile_version=spec["recheck_profile_version"])

    def bind(self, principal, plan_id, context_id, context, *, expected_revision,
             gate_phase="initial", application_id=None):
        """Bind the registered profile. Retrying an old binding never reactivates it."""
        identity(context_id)
        if not isinstance(context, GateContext):
            raise ValueError("invalid_gate_context")
        if type(expected_revision) is not int or not 0 <= expected_revision < 10**12:
            raise ValueError("invalid_gate_revision")
        if gate_phase not in {"initial", "post_apply"}:
            raise ValueError("invalid_gate_phase")
        if application_id is not None:
            identity(application_id)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = self._plan(c, principal, plan_id, "gate:bind")
            spec = json.loads(plan["spec_json"])
            if (context.tenant_id != plan["tenant_id"] or context.app != spec["workload_id"]
                    or context.plan_digest != spec["plan_digest"]
                    or not _profile_matches(c, plan, context, gate_phase, application_id)):
                raise EventError("gate_context_scope_mismatch")
            for side in ("source", "target"):
                record = _get(c, plan_id, "run", getattr(context, side + "_ref"))
                expected_env = dict(id=getattr(context, side + "_environment_id"), role=side,
                                    ref=getattr(context, side + "_environment_ref"))
                if (record is None or record["side"] != side
                        or record["digest"] != getattr(context, side + "_digest")
                        or record["document"]["environment"] != expected_env
                        or expected_env["id"] != spec[side + "_environment_id"]):
                    raise EventError("gate_run_unavailable_or_changed")
            value = dict(context=asdict(context), expected_revision=expected_revision,
                         revision=expected_revision + 1, context_id=context_id)
            if gate_phase == "post_apply":
                value.update(gate_phase=gate_phase, application_id=application_id)
            old = _get(c, plan_id, "context", context_id)
            if old is not None:
                if old != value:
                    raise EventError("gate_record_conflict")
                return old
            current = _current(c, plan_id)
            if gate_phase == "post_apply" and plan["state"] != "GATE":
                raise EventError("gate_stage_required")
            if (current["revision"] if current else 0) != expected_revision:
                raise EventError("version_conflict")
            _put(c, plan_id, "context", context_id, value)
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (
                plan_id, f"gate-current-{value['revision']:013}", "{}", _canonical(value)))
            return value

    def view(self, connection, principal, plan):
        """Use the caller's locked plan connection, including CutoverService's.

        The returned providers do not open another connection or grant trust.
        Build T19 with its mandatory authentication/authorization providers.
        """
        current = self._plan(connection, principal, plan["plan_id"], "gate:read")
        if current != dict(plan):
            raise EventError("gate_plan_changed")
        binding = _current(connection, plan["plan_id"])
        if binding is None:
            raise EventError("gate_context_unavailable")
        return GateRecordView(self, connection, principal, current, binding)

    def evaluate(self, principal, plan_id, *, service_factory, approval_ids=()):
        """Evaluate with the existing T19 service and save both original reports."""
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = self._plan(c, principal, plan_id, "gate:evaluate")
            view = self.view(c, principal, plan)
            service = service_factory(view)
            if not isinstance(service, CombinedGateService):
                raise ValueError("combined_gate_service_required")
            document = service.evaluate(principal.subject_id, view.context_id,
                                        approval_ids=approval_ids)
            view._check(principal.subject_id, view.context)
            self._plan(c, principal, plan_id, "gate:evaluate")
            if (document["context_digest"] != view.context.fingerprint
                    or document["security"]["gate_id"] != view.context.gate_id):
                raise EventError("gate_report_scope_mismatch")
            scope = _readiness_scope(service, principal.subject_id, view.context)
            if scope["binding"] != document["readiness_binding_digest"]:
                raise EventError("gate_report_scope_mismatch")
            _put(c, plan_id, "security-report", view.context.gate_id, document["security"])
            _put(c, plan_id, "combined-report", view.context.gate_id, document)
            _put(c, plan_id, "readiness-scope", view.context.gate_id, scope)
            return document


class GateRecordView:
    """Connection-bound T19 providers; never return these beyond the transaction."""

    def __init__(self, records, connection, principal, plan, binding):
        self.records, self.c, self.principal, self.plan = records, connection, principal, plan
        self.binding = binding
        self.context_id = binding["context_id"]
        self.context = GateContext(**binding["context"])
        self.gate_phase = binding.get("gate_phase", "initial")
        self.application_id = binding.get("application_id")
        if not _profile_matches(connection, plan, self.context, self.gate_phase, self.application_id):
            raise EventError("gate_context_scope_mismatch")

    def _check(self, actor, context):
        latest = self.records._plan(self.c, self.principal, self.plan["plan_id"], "gate:read")
        if (actor != self.principal.subject_id or context != self.context or latest != self.plan
                or _current(self.c, self.plan["plan_id"]) != self.binding):
            raise EventError("gate_context_changed")

    def context_provider(self, actor, context_id):
        self._check(actor, self.context)
        if context_id != self.context_id:
            raise EventError("gate_context_unavailable")
        return self.context

    def run_provider(self, actor, context, side):
        self._check(actor, context)
        if side not in {"source", "target"}:
            raise EventError("gate_run_scope_mismatch")
        ref = getattr(context, side + "_ref")
        record = _get(self.c, self.plan["plan_id"], "run", ref)
        if record is None or record["side"] != side:
            raise EventError("gate_run_unavailable_or_changed")
        snapshot = RunSnapshot(ref, canonical(record["document"]))
        if snapshot.fingerprint != getattr(context, side + "_digest"):
            raise EventError("gate_run_unavailable_or_changed")
        return snapshot

    def report_provider(self, actor, context):
        self._check(actor, context)
        served = self._served(context)
        doc = (served["security"] if served is not None
               else _get(self.c, self.plan["plan_id"], "security-report", context.gate_id))
        return RunSnapshot(context.gate_id, canonical(doc)) if doc is not None else None

    def _served(self, context):
        """The platform re-evaluation, only inside served_reevaluation for this transaction."""
        value = _SERVED.get()
        if value is None or value[0] is not self.c or value[1:3] != (self.plan["plan_id"], context.gate_id):
            return None
        return value[3]

    def reevaluation_authority(self, context, subject):
        """Provenance of a platform re-evaluation (sangmu1126 #565 ②): the server record computed in
        this transaction from inputs that each passed their own signature checks. Never exported as a
        signed gate-result; outside served_reevaluation this returns None."""
        served = self._served(context)
        if served is None or RunSnapshot(context.gate_id, canonical(served["security"])).fingerprint != subject:
            return None
        return VerifiedAuthority(context.fingerprint, subject, "gate-result",
                                 time(served["evaluated_at"]) + timedelta(seconds=60))

    def cutover_binding(self, binding):
        """Add the stored phase to the host's current human-approval binding.

        The upstream T10 authority calls this before T19 checks current.binding.
        Never relabel a binding that already identifies a different application.
        """
        self._check(self.principal.subject_id, self.context)
        expected = dict(plan_id=self.plan["plan_id"], plan_digest=self.context.plan_digest,
                        target_environment_id=self.context.target_environment_id,
                        gate_id=self.context.gate_id, context_digest=self.context.fingerprint)
        phase = dict(gate_phase=self.gate_phase, application_id=self.application_id)
        if (any(binding.get(key) != value for key, value in expected.items())
                or any(key in binding and binding[key] != value for key, value in phase.items())):
            raise EventError("gate_context_scope_mismatch")
        return dict(binding) | phase

    def session(self, service, readiness, *, approval_ids=()):
        self._check(self.principal.subject_id, self.context)
        doc = _get(self.c, self.plan["plan_id"], "combined-report", self.context.gate_id)
        if doc is None:
            raise EventError("gate_report_unavailable")
        phase = {} if self.gate_phase == "initial" else dict(
            gate_phase=self.gate_phase, application_id=self.application_id)
        return PlanGateSession(service, self.plan["plan_id"], self.context_id, self.context,
                               RunSnapshot(self.context.gate_id, canonical(doc)), readiness,
                               approval_ids, **phase)
