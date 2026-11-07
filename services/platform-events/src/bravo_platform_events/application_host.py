"""Trusted T10 host for the T31 library; no HTTP apply or proof enrollment route.

The durable T10 claim is committed before cluster I/O. An existing claim only
reads T31's record, including when a crash left no record at all.
"""

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from bravo_apply import apply, get_result
from bravo_apply.path import GATE_RECHECK_TESTS, ApplyOutcome

from .change import ChangeApprovalService
from .cutover import _get
from .executor import _timestamp
from .plan_api import authorized_plan
from .plans import change_key
from .store import EventError
from .workflow import Application, Change, Evidence, Gate


def _post_apply(view, outcome):
    """A pre-change target run cannot satisfy the requested recheck."""
    if view.gate_phase != "post_apply" or view.application_id != outcome.application_id:
        raise EventError("application_gate_binding_mismatch")
    profile = view.records.contract.profile(view.context.profile_id, view.context.profile_version)
    if not set(GATE_RECHECK_TESTS) <= set(profile["required_test_ids"]):
        raise EventError("gate_recheck_profile_incomplete")
    target = view.run_provider(view.principal.subject_id, view.context, "target").document
    finished = _timestamp(outcome.finished_at)
    if _timestamp(target["started_at"]) < finished:
        raise EventError("post_application_target_run_required")
    results = {row["test_id"]: row for row in target["results"]}
    if any(test not in results or _timestamp(results[test]["evidence"]["collected_at"]) < finished
           for test in GATE_RECHECK_TESTS):
        raise EventError("post_application_controls_required")


@dataclass(frozen=True)
class ApplyInput:
    """Registered target and immutable candidate, loaded by deployment-owned code.

    load_input(connection, principal, plan, claim) must check the current target
    registration and load the exact T15 candidate. Client JSON is not a supplier.
    The T31 library independently checks the candidate bytes against the claim.
    """

    proposal: dict
    diff: bytes
    target_environment_id: str
    namespace: str
    deployment: str
    container: str
    repository: str
    pull_secret: str | None = None  # T49: registry Secret the candidate is pulled with (None: node image)


class ApplicationHost:
    """One process per retained receipt volume, serialized effects within it.

    Keep this instance alive for the host's lifetime. A second instance fails
    rather than sharing the execution role. Closing waits for an active call.
    A local filesystem with flock, fsync, atomic link/replace is required.
    """

    def __init__(self, changes, *, kube, receipts_dir, load_input):
        if (not isinstance(changes, ChangeApprovalService) or not callable(load_input)
                or not callable(changes.check_access)):
            raise ValueError("trusted_application_ports_required")
        root = Path(receipts_dir)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValueError("persistent_receipts_mount_required")
        self._mutex = RLock()
        self._fd = os.open(root / ".host.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._fd)
            raise
        self.changes, self.kube, self.root, self.load_input = changes, kube, root, load_input

    def close(self):
        with self._mutex:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start(self, principal, plan_id, *, approval_id, command_key):
        """First committed admission may apply. Every retry is read-only.

        None means a claim exists but execution was not observed. It is a hold,
        never permission to call apply again. Exceptions also require readback.
        """
        with self._mutex:
            if self._fd is None:
                raise RuntimeError("application_host_closed")
            claim, created = self.changes.claim_application(principal, plan_id,
                approval_id=approval_id, command_key=command_key)
            if not created:
                return get_result(claim["application_id"], self.root)
            # Recheck authority after admission and before loading the candidate.
            # This transaction ends before T31 performs any Kubernetes call.
            with self.changes.store._transaction(write=True, plan_id=plan_id) as c:
                plan = authorized_plan(c, principal, plan_id, self.changes.check_access)
                approved = _get(c, plan_id, change_key("approval", approval_id))[1]
                current = self.changes._current(c, principal, plan, self.changes.clock(),
                    rehearsal_id=claim["rehearsal_id"], approved_by=approved["actor_id"],
                    approval_id=approval_id)
                if (current.binding != claim["binding"]
                        or current.verification_id != claim["verification_id"]
                        or current.revision != claim["context_revision"]
                        or approved["plan_version"] != plan["version"]):
                    raise EventError("application_authority_changed")
                supplied = self.load_input(c, principal, plan, claim)
                if not isinstance(supplied, ApplyInput):
                    raise EventError("application_input_unavailable")
                # Detach provider-owned mutable JSON before leaving the lock.
                proposal = json.loads(json.dumps(supplied.proposal, allow_nan=False))
                if (type(supplied.diff) is not bytes
                        or supplied.target_environment_id != claim["binding"]["target_environment_id"]):
                    raise EventError("application_input_scope_mismatch")
            return apply(claim["application_id"], claim, proposal, kube=self.kube,
                receipts_dir=self.root, target_environment_id=supplied.target_environment_id,
                namespace=supplied.namespace, deployment=supplied.deployment,
                container=supplied.container, repository=supplied.repository, diff=supplied.diff,
                pull_secret=supplied.pull_secret)


class GateRecheck:
    """T10 calls the existing T19 evaluator only after a real T31 APPLIED result.

    prepare(principal, plan_id, outcome) collects/authenticates fresh post-apply
    source/target records and binds them through GateRecords. It must be
    idempotent by application_id and preserve an existing valid recheck.
    Missing producer records must raise. It must never copy pre-apply evidence
    or turn the requested control IDs into PASS. No default provider is supplied.
    """

    def __init__(self, records, *, prepare, service_factory):
        from .gate_records import GateRecords

        if not isinstance(records, GateRecords) or not all(map(callable, (prepare, service_factory))):
            raise ValueError("trusted_gate_ports_required")
        self.records, self.prepare, self.service_factory = records, prepare, service_factory

    def evaluate(self, principal, plan_id, outcome):
        if (not isinstance(outcome, ApplyOutcome) or outcome.plan_id != plan_id
                or outcome.cluster != "kubectl" or outcome.t10_application() is None
                or not outcome.finished_at or outcome.gate_recheck is None):
            raise EventError("real_application_required")
        # The producer sets the request shape; do not accept a partial list.
        if outcome.gate_recheck != dict(plan_id=plan_id, application_id=outcome.application_id,
                image_digest=outcome.deployed_digest, tests=GATE_RECHECK_TESTS):
            raise EventError("gate_recheck_request_mismatch")
        self.records.recheck_profile(principal, plan_id, outcome.application_id)
        self.prepare(principal, plan_id, outcome)
        def checked_factory(view):
            if view.plan["state"] != "GATE":
                raise EventError("gate_stage_required")
            _post_apply(view, outcome)
            return self.service_factory(view)
        return self.records.evaluate(principal, plan_id, service_factory=checked_factory)


def _canonical_report(value):
    from bravo_gate.contract import canonical

    return canonical(value)


class ApplicationEvidence:
    """Read-only WorkflowRecovery port for one server-selected application.

    check_current(c, principal, plan, outcome) must return True only for current
    grants, holds, target registration and authenticated deployed-image evidence.
    load_gate(view) returns (CombinedGateService, ReadinessBinding) using T19's
    mandatory current provenance/permission providers. No network I/O under the
    plan lock. These ports have no permissive default and are not client inputs.
    """

    def __init__(self, *, principal, application_id, receipts_dir, records,
                 check_current, load_gate, fallback):
        if not all(map(callable, (check_current, load_gate, fallback))):
            raise ValueError("current_application_authorities_required")
        self.principal, self.application_id, self.root = principal, application_id, Path(receipts_dir)
        self.records, self.check_current, self.load_gate, self.fallback = records, check_current, load_gate, fallback

    def __call__(self, c, plan, previous, run):
        if plan["state"] not in {"REHEARSE", "GATE"}:
            return self.fallback(c, plan, previous, run)
        outcome = get_result(self.application_id, self.root)
        if (outcome is None or outcome.cluster != "kubectl" or outcome.plan_id != plan["plan_id"]
                or plan["tenant_id"] != self.principal.tenant_id
                or outcome.t10_application() is None or not outcome.finished_at):
            raise EventError("real_application_required")
        saved = _get(c, plan["plan_id"], change_key("consumed", outcome.approval_id))
        if saved is None:
            raise EventError("application_claim_required")
        claim = saved[1]
        spec = json.loads(plan["spec_json"])
        if (claim["application_id"] != outcome.application_id
                or claim["verification_id"] != outcome.verification_id
                or claim["binding"]["plan_digest"] != spec["plan_digest"]
                or claim["binding"]["target_environment_id"] != spec["target_environment_id"]
                or claim["binding"]["image_digest"] != outcome.deployed_digest
                or claim["binding"]["diff_hash"] != outcome.change["diff_hash"]
                or self.check_current(c, self.principal, plan, outcome) is not True):
            raise EventError("application_authority_unavailable")
        change = Change(**outcome.change)
        receipt_id = outcome.application_id
        if plan["state"] == "REHEARSE":
            fields = outcome.t10_application() | {"change": change}
            result = Application(**fields)
        else:
            if not previous or previous["result"].get("application_id") != outcome.application_id:
                raise EventError("application_checkpoint_mismatch")
            view = self.records.view(c, self.principal, plan)
            _post_apply(view, outcome)
            service, readiness = self.load_gate(view)
            session = view.session(service, readiness)
            # The stored GATE report is valid for 60 s and its gate-result signature comes from an
            # operator machine, so a later sweep re-evaluates under the same scope rule as SHIFT and
            # cutover (served_reevaluation): same context, policy, DB pair, snapshot not older.
            from .gate_records import served_reevaluation

            with served_reevaluation(c, plan["plan_id"], session, self.principal) as fresh:
                proof = service.require_prerequisites(self.principal.subject_id,
                    session.context_id, _canonical_report(fresh), approval_ids=session.approval_ids)
            if (proof.context_digest != view.context.fingerprint
                    or proof.readiness_binding_digest != readiness.fingerprint):
                raise EventError("gate_report_scope_mismatch")
            # Recheck provider bindings/grants after the evaluator, before commit.
            again = self.records.view(c, self.principal, plan)
            next_service, next_readiness = self.load_gate(again)
            if (again.session(next_service, next_readiness).fingerprint != session.fingerprint
                    or self.check_current(c, self.principal, plan, outcome) is not True
                    or service.security.clock() >= _timestamp(proof.valid_until)):
                raise EventError("application_gate_authority_changed")
            result = Gate(change, view.context.gate_id, proof.context_digest, "PASS")
            receipt_id = view.context.gate_id
        return Evidence(plan["plan_id"], spec["plan_digest"], spec["target_environment_id"],
                        plan["version"], receipt_id, result)
