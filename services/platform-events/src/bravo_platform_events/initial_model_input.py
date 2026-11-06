"""Pure compilation of an admitted source into the existing T11 input contract.

Admission, dispatch preparation and the broker use this same compiler. It does
not shorten analysis, alter stored input, acquire authority or claim execution.
"""

from dataclasses import dataclass

from bravo_agent.contracts import FailureBundle, SnapshotManifest
from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.credential_proposer import credential_summary
from rehearsal_runner.secret_task import prepare_credential_migration


@dataclass(frozen=True)
class PreparedInitialInput:
    bundle: FailureBundle
    manifest: SnapshotManifest
    files: dict[str, str]


def model_input(value):
    if (value.schema_version != 2 or value.analysis.grade != "code_change"
            or value.analysis.inventory_digest != value.binding.inventory_digest
            or value.binding.stage != "PROPOSE" or value.binding.purpose != "t38-initial-proposal"
            or value.manifest.source_sha != value.binding.source_sha):
        raise ValueError("initial_model_input_conflict")
    # The inventory explains why a change is needed; the registered task fixes
    # the destination interface. Never ask the model to guess the injected path.
    task = prepare_credential_migration(value.run_id, value.binding.source_sha)
    if logical_task_digest(task) != value.binding.task_digest:
        raise ValueError("initial_task_contract_unavailable")
    bundle = FailureBundle(run_id=value.run_id, source_sha=value.binding.source_sha,
        failure_code="CODE_CHANGE_REQUIRED",
        summary=credential_summary(task) + "\nInventory finding: " + value.analysis.rationale,
        evidence_digest=value.analysis.inventory_digest)
    # No admission_digest is invented. Only T11's existing worker value shape is used.
    return PreparedInitialInput(bundle=bundle, manifest=value.manifest, files=value.files)
