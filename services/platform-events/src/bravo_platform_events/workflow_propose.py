"""PROPOSE for the workflow host: the T38 Job result plus the platform-built candidate.

Out of the plan lock: read the saved input/intent/result (current grant rechecked) and let the platform
build and push the candidate (candidate_build, T49 2026-11-08: no GitHub in the happy path). The returned
reader only checks locally; the existing evidence_reader rechecks authority under the lock. The host never
approves, and no candidate code runs while the image is built.
"""

import base64
import json

from .candidate_build import PlatformCandidateBuilder
from .cutover import _get
from .initial_proposal import InitialProposalExecution, _admission_key
from .store import EventError


def apply_input_key(diff_hash):
    return "workflow-apply-input-" + diff_hash.removeprefix("sha256:")


def record_apply_input(store, plan_id, value, result):
    """Keep the exact edits and T15/T31 diff bytes the change approval binds (diff_hash), so REHEARSE
    hands T31 the same candidate later. Write-once; the same hash always means the same bytes."""
    from bravo_apply.binding import diff_hash, render_diff

    edits = [edit.model_dump(mode="json") for edit in result.proposal.edits]
    diff = render_diff(dict(value.files), edits)
    record = {"edits": edits, "diff_b64": base64.b64encode(diff).decode(), "diff_hash": diff_hash(diff),
              "infra_plans": list(result.infra_plans)}  # ADR-0044: as the agent returned them, df's binding selects
    key = apply_input_key(record["diff_hash"])
    with store._transaction(write=True, plan_id=plan_id) as c:
        if c.execute("SELECT 1 FROM plan_commands WHERE plan_id=? AND command_key=?", (plan_id, key)).fetchone() is None:
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (plan_id, key, "{}", json.dumps(record, sort_keys=True)))
    return record


def propose_preparer(execution, *, build, auto=None):
    """`build` (PlatformCandidateBuilder) turns the stored Proposal into the publication T10 binds.

    `auto` (initial_proposal_auto.AutoInitialProposal, T49) admits the plan's one registered task and runs
    the AI Job itself; without it an operator does both (operations/initial-proposal/run.py)."""
    if type(execution) is not InitialProposalExecution or type(build) is not PlatformCandidateBuilder:
        raise ValueError("propose_ports_required")

    def admitted_ref(plan_id, version):
        with execution.records.store._transaction() as c:
            return _get(c, plan_id, _admission_key(c, plan_id, version))

    def prepare(plan_id, tenant_id, version):
        run_ai = auto if auto is not None and auto.covers(plan_id) else None
        admitted = admitted_ref(plan_id, version)
        if not admitted and run_ai is not None:
            auto.admit(plan_id, tenant_id, version)
            admitted = admitted_ref(plan_id, version)
        if not admitted:
            raise EventError("initial_execution_not_complete")
        if run_ai is not None:
            run_ai(plan_id, version, admitted[1])  # holds as initial_proposal_running until the Job's result is stored
        value, intent, result = execution.publication_inputs(plan_id, version, **admitted[1])
        publication = build.publication(value, intent, result)  # EventError holds and retries next sweep
        record_apply_input(execution.records.store, plan_id, value, result)
        return execution.evidence_reader(publication=publication)

    return prepare
