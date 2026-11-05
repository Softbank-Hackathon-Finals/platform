"""Host-only T15 intake. No network route or evidence source is installed by default."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .change import CurrentChange, change_key, intake_binding
from .contract import validate_identifier
from .cutover import _get, _put
from .executor import _timestamp
from .plans import _canonical, _digest
from .rehearsal import require_admitted_delivery, require_current_run
from .store import EventError
from .workload_identity import WorkloadSender


@dataclass(frozen=True)
class Candidate:
    plan_id: str
    run_id: str
    attempt: int
    verification_id: str
    evidence_ref: str
    target_environment_id: str
    namespace: str
    proposal_digest: str
    diff_hash: str
    image_digest: str

    def validate(self):
        for value in (self.plan_id, self.run_id, self.verification_id, self.evidence_ref,
                      self.target_environment_id, self.namespace):
            validate_identifier(value)
        if type(self.attempt) is not int or not 1 <= self.attempt <= 999999999:
            raise ValueError("invalid_attempt")
        _digest(self.proposal_digest)
        if any(not isinstance(v, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", v)
               for v in (self.diff_hash, self.image_digest)):
            raise ValueError("invalid_artifact_digest")


@dataclass(frozen=True)
class SourceVerification:
    """Trusted reader output: candidate digest fields come from verified artifacts.

    The host must read an authenticated original at an opaque, scoped reference;
    copying any submitted field into this record is not verification.
    """

    candidate: Candidate
    tenant_id: str
    plan_digest: str
    plan_version: int
    expires_at: str
    verdict: str
    created_uid: str
    removed_uid: str
    absence_confirmed: bool


def _latest(c, plan_id, run_id):
    prefix = change_key("intake-attempt", run_id) + "-"
    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                    "ORDER BY command_key DESC LIMIT 1", (plan_id, prefix + "%")).fetchone()
    return json.loads(row["receipt_json"]) if row else None


def _plan(c, plan_id, tenant):
    row = c.execute("SELECT plan_id, tenant_id, spec_json, state, version FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None or row["tenant_id"] != tenant:
        raise EventError("forbidden")
    return row


class ChangeIntake:
    """One tenant per sender binding. Host passes separate, trusted hold authority."""

    def __init__(self, store, *, sender: WorkloadSender, read_original: Callable[[str, str, str, str], SourceVerification],
                 authorize_hold, clock=None):
        if not callable(read_original) or not callable(authorize_hold):
            raise ValueError("intake_authorities_required")
        self.store, self.sender, self.read_original = store, sender, read_original
        self.authorize_hold = authorize_hold
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def receive(self, token, candidate: Candidate):
        # Authenticate before fetching even an opaque evidence reference. Do not pass
        # the submitted payload to the reader: it supplies independent artifact data.
        self.sender.authenticate(token)
        if type(candidate) is not Candidate:
            raise ValueError("invalid_candidate")
        candidate.validate()
        p, r, v = candidate.plan_id, candidate.run_id, candidate.verification_id
        with self.store._transaction() as c:
            _plan(c, p, self.sender.tenant_id)
        try:
            original = self.read_original(p, r, v, candidate.evidence_ref)
            if (type(original) is not SourceVerification or type(original.candidate) is not Candidate or original.candidate != candidate
                    or original.tenant_id != self.sender.tenant_id or original.verdict != "PASS"
                    or original.absence_confirmed is not True or original.created_uid != original.removed_uid):
                raise ValueError()
            original.candidate.validate()
            validate_identifier(original.created_uid)
            if type(original.plan_version) is not int or original.plan_version < 1:
                raise ValueError()
            _digest(original.plan_digest)
            expiry = _timestamp(original.expires_at)
        except Exception:
            raise EventError("authority_unavailable") from None
        record = asdict(original)
        revision = hashlib.sha256(_canonical(record).encode()).hexdigest()
        with self.store._transaction(write=True, plan_id=p) as c:
            self.sender.authenticate(token)  # may have expired while fetching or waiting
            plan = _plan(c, p, self.sender.tenant_id)
            spec = json.loads(plan["spec_json"])
            if (spec["plan_digest"] != original.plan_digest
                    or spec["target_environment_id"] != candidate.target_environment_id):
                raise EventError("binding_mismatch")
            if _get(c, p, change_key("intake-hold", v)):
                raise EventError("verification_held")
            key = change_key("intake-proof", v)
            previous = _get(c, p, key)
            if previous:
                if previous[0] != record:
                    raise EventError("verification_conflict")
                # Historical delivery acknowledgment, never an approval or reactivation.
                return previous[1]
            if original.plan_version != plan["version"]:
                raise EventError("version_conflict")
            if plan["state"] != "REHEARSE" or expiry <= _timestamp(self.clock()):
                raise EventError("candidate_unavailable")
            require_admitted_delivery(c, plan, r, candidate.attempt)
            latest = _latest(c, p, r)
            if latest and latest["attempt"] >= candidate.attempt:
                raise EventError("attempt_conflict")
            receipt = dict(verification_id=v, revision=revision, attempt=candidate.attempt)
            _put(c, p, key, record, receipt)
            _put(c, p, change_key("intake-attempt", r) + f"-{candidate.attempt:09d}", {}, receipt)
            return receipt

    def hold(self, credential, plan_id, verification_id, *, decision_ref):
        """Trusted management port, not a sender action. The host policy must verify its
        own caller and permission for this plan/verification inside this DB lock.
        A hold can precede delivery and cannot be cleared by replaying a PASS.
        """
        for value in (plan_id, verification_id, decision_ref):
            validate_identifier(value)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            _plan(c, plan_id, self.sender.tenant_id)
            if self.authorize_hold(
                    c, credential, plan_id, verification_id, decision_ref) is not True:
                raise EventError("forbidden")
            key = change_key("intake-hold", verification_id)
            previous = _get(c, plan_id, key)
            if previous:
                return previous[1]
            receipt = dict(verification_id=verification_id, revision="held-" + hashlib.sha256(
                _canonical([plan_id, verification_id, decision_ref]).encode()).hexdigest())
            _put(c, plan_id, key, {"decision_ref": decision_ref}, receipt)
            return receipt

    @staticmethod
    def current(c, principal, plan, *, rehearsal_id, **_kwargs):
        """Compose with OperatorAccess.change_authority. No I/O outside this transaction."""
        p = plan["plan_id"]
        head = _latest(c, p, rehearsal_id)
        if not head or _get(c, p, change_key("intake-hold", head["verification_id"])):
            raise EventError("authority_unavailable")
        record = require_current_run(c, plan, rehearsal_id, head["verification_id"])
        if record is None:  # Candidate from before managed rehearsal admission.
            record, _ = _get(c, p, change_key("intake-proof", head["verification_id"]))
        if record["plan_version"] != plan["version"]:
            raise EventError("authority_unavailable")
        return CurrentChange(intake_binding(record), rehearsal_id, head["verification_id"], head["revision"])
