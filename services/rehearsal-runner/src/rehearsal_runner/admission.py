"""Run one T15 attempt from T10's committed host admission.

Only the trusted T10 host may invoke this entry point. The host must authenticate
its caller, provide the committed Admission and ensure submit-once/recovery;
constructing an Admission object in another process does not grant authority.
"""

import hashlib
import json
import re

from .bucket_task import BucketPolicyTask, prepare_bucket_policy_task
from .retry import AttemptFactory, AttemptPorts
from .runner import AttemptResult, run_once
from .secret_task import CredentialMigrationTask, prepare_credential_migration


def logical_task_digest(task: object) -> str:
    """Bind a logical task across attempts without including its execution ID."""
    if type(task) is BucketPolicyTask:
        if task != prepare_bucket_policy_task(task.run_id, task.source_policy, task.target_bucket,
                                              source_sha=task.source_sha):
            raise ValueError("invalid bucket task")
        kind = "bucket-policy"
    elif type(task) is CredentialMigrationTask:
        if task != prepare_credential_migration(task.run_id, task.source_sha):
            raise ValueError("invalid credential task")
        kind = "credential-migration"
    else:
        raise ValueError("unsupported rehearsal task")
    public_input = task.as_model_input()
    del public_input["run_id"]
    raw = json.dumps({"kind": kind, "task": public_input}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def run_admitted_attempt(admission: object, task: object, factory: AttemptFactory, *,
                         feedback_digest: str | None = None) -> AttemptResult:
    """Execute exactly the committed attempt; never initiate a later attempt.

    Retry feedback must already be authenticated and resolved by the trusted
    host. T10 owns plan locking, admission, retry numbering and the previous
    hold; T15 still requires durable submit deduplication and recovery.
    """
    from bravo_platform_events.rehearsal import Admission

    try:
        if (type(admission) is not Admission or type(admission.attempt) is not int
                or not 1 <= admission.attempt <= 3
                or getattr(task, "run_id", None) != admission.rehearsal_id
                or getattr(task, "source_sha", None) != admission.source_sha
                or logical_task_digest(task) != admission.task_digest):
            return AttemptResult("BLOCK", "ADMISSION_MISMATCH")
        if (admission.attempt == 1 and feedback_digest is not None
                or admission.attempt > 1 and (not isinstance(feedback_digest, str)
                    or re.fullmatch(r"[a-f0-9]{64}", feedback_digest) is None)):
            return AttemptResult("BLOCK", "FEEDBACK_MISMATCH")
        ports = factory.prepare(admission.attempt, feedback_digest)
        if (not isinstance(ports, AttemptPorts)
                or type(getattr(ports.verifier, "attempt", None)) is not int
                or ports.verifier.attempt != admission.attempt
                or (hasattr(ports.lifecycle, "attempt")
                    and (type(ports.lifecycle.attempt) is not int
                         or ports.lifecycle.attempt != admission.attempt))):
            return AttemptResult("BLOCK", "ADMISSION_PORT_MISMATCH")
    except Exception:
        return AttemptResult("NEEDS_HUMAN", "ADMISSION_CHECK_UNAVAILABLE")
    if ports.exec_phase is not None:
        from .exec_flow import run_with_exec

        return run_with_exec(task, ports.proposer, ports.exec_phase, ports.lifecycle, ports.isolation,
                             ports.verifier, admission.attempt)
    return run_once(task, ports.proposer, ports.lifecycle, ports.isolation, None,
                    isolated_verifier=ports.verifier)
