"""T15 host adapter for T10's committed rehearsal admission.

Catalog, sender and evidence reader are trusted host dependencies. This module
installs no network route, model authority, evidence authentication or durable
remote submitter; the host must configure those before enabling T10 admission.
"""

from dataclasses import dataclass, field, replace
from typing import Protocol

from .admission import logical_task_digest
from .bucket_task import BucketPolicyTask
from .secret_task import CredentialMigrationTask

Task = BucketPolicyTask | CredentialMigrationTask


class TaskCatalog(Protocol):
    def for_plan(self, connection: object, plan: dict, task_id: str) -> Task:
        """Read the registered logical task using T10's plan transaction."""
        ...

    def for_admission(self, admission: object) -> Task:
        """Read the same immutable registered task after admission commits."""
        ...


class AttemptSender(Protocol):
    def submit_once(self, admission: object, task: Task) -> None:
        """Durably deduplicate remote submission by rehearsal ID and attempt."""
        ...


class EvidenceReader(Protocol):
    def read_authenticated(self, admission: object) -> object | None:
        """Read original authenticated T38 result and UID cleanup proof, never model output."""
        ...


@dataclass(frozen=True)
class T15RehearsalHost:
    """Adapter to the existing T10 RehearsalHost protocol, not a new wire contract."""

    catalog: TaskCatalog = field(repr=False)
    sender: AttemptSender = field(repr=False)
    evidence: EvidenceReader = field(repr=False)

    def task(self, connection: object, plan: dict, task_id: str):
        from bravo_platform_events.rehearsal import RehearsalTask

        task = self.catalog.for_plan(connection, plan, task_id)
        digest = logical_task_digest(task)
        return RehearsalTask(task.source_sha, digest)

    def submit(self, admission: object) -> None:
        from bravo_platform_events.rehearsal import Admission

        if type(admission) is not Admission:
            raise ValueError("invalid admission")
        source = self.catalog.for_admission(admission)
        if (type(source) not in (BucketPolicyTask, CredentialMigrationTask)
                or source.source_sha != admission.source_sha
                or logical_task_digest(source) != admission.task_digest):
            raise ValueError("admitted task changed")
        task = replace(source, run_id=admission.rehearsal_id)
        if logical_task_digest(task) != admission.task_digest:
            raise ValueError("admitted task changed")
        # No retry on exception: T10 keeps the committed attempt unconfirmed.
        self.sender.submit_once(admission, task)

    def observe(self, admission: object):
        from bravo_platform_events.rehearsal import Admission, Observation

        if type(admission) is not Admission:
            raise ValueError("invalid admission")
        result = self.evidence.read_authenticated(admission)
        if result is None:
            return None
        if (type(result) is not Observation or result.admission != admission
                or result.verdict not in ("PASS", "FAIL") or result.absence_confirmed is not True
                or not isinstance(result.created_uid, str) or not result.created_uid.strip()
                or result.created_uid != result.removed_uid):
            raise ValueError("unbound verification evidence")
        # T10 also verifies the source and holds under its plan lock.
        return result
