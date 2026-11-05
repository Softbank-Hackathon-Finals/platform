"""Adapt a committed first Proposal to T15's existing planner port, without a model.

The normal T11 coordinator revalidates the stored proposal against the exact
input snapshot. Its replay is not the remote model's tool audit. Every result
read rechecks T10's current Admission, publication and purpose grant.
"""

from dataclasses import dataclass, field

from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot
from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.secret_task import CredentialMigrationTask

from .initial_proposal import InitialProposalExecution
from .product_source import CredentialSnapshotSource
from .rehearsal import Admission


@dataclass(frozen=True)
class _StoredRecord:
    execution: InitialProposalExecution = field(repr=False)
    admission: Admission
    snapshot: Snapshot = field(repr=False)
    original: dict = field(repr=False)

    def read(self):
        record = self.execution.rehearsal_record(self.admission)
        value = record["input"]
        if (record != self.original
                or self.snapshot.manifest.model_dump(mode="json") != value["manifest"]
                or {path: self.snapshot.read(path) for path in value["files"]} != value["files"]):
            raise ValueError("stored_initial_planner_input_changed")
        return record["result"]


@dataclass(frozen=True)
class _StoredProvider:
    record: _StoredRecord = field(repr=False)
    bundle: object = field(repr=False)
    tool_events_available = False

    async def propose(self, bundle, tools):
        if tools.attempt != 1 or bundle != self.bundle:
            raise ValueError("stored_initial_planner_first_attempt_required")
        result = self.record.read()
        tools.dispatch("mcp__bravo__plan_propose", result["proposal"])


class _CurrentCoordinator:
    """Delegate T11 behavior and recheck authority when T15 reads the proposal."""

    def __init__(self, coordinator, record):
        self._coordinator, self._record = coordinator, record

    def __getattr__(self, name):
        return getattr(self._coordinator, name)

    async def propose(self, provider):
        await self._coordinator.propose(provider)
        return self.result()

    def result(self):
        stored = self._record.read()
        result = self._coordinator.result()
        if result.get("proposal") is not None and any(
                result.get(key) != stored[key]
                for key in ("run_id", "source_sha", "attempt", "proposal", "proposal_digest")):
            raise ValueError("stored_initial_planner_proposal_changed")
        return result


@dataclass(frozen=True)
class StoredInitialCredentialSource:
    """T15 ProposalSource.open for the first actual credential Admission only.

    The host supplies the immutable source snapshot used by InitialInput. A
    reconstructed task or source with different binding is refused before the
    planner can create a namespace. Later attempts need their own producer.
    """

    execution: InitialProposalExecution = field(repr=False)
    snapshot: Snapshot = field(repr=False)

    def open(self, admission, task):
        if (type(self.execution) is not InitialProposalExecution
                or type(admission) is not Admission or type(task) is not CredentialMigrationTask
                or not isinstance(self.snapshot, Snapshot)
                or task.run_id != admission.rehearsal_id or task.source_sha != admission.source_sha
                or logical_task_digest(task) != admission.task_digest):
            raise ValueError("stored_initial_planner_binding_required")
        record = _StoredRecord(self.execution, admission, self.snapshot,
                               self.execution.rehearsal_record(admission))
        record.read()
        source = CredentialSnapshotSource(self.snapshot)(None, None, admission, task)
        bundle = source["bundle"]
        coordinator = _CurrentCoordinator(Coordinator(bundle, self.snapshot), record)
        return coordinator, _StoredProvider(record, bundle)
