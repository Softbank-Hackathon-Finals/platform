"""PROPOSE for the workflow host: the T38 Job result plus a reviewed T7 publication.

Out of the plan lock: read the saved input/intent/result (current grant rechecked),
take the operator-pinned PublicationSelection for that request and authenticate the
existing T7 run with GitHub GETs. The returned reader only checks locally; the
existing evidence_reader rechecks authority under the lock. The host never builds or approves:
with an operator pin it only reads; without one, `writer` (CandidateWriter, GitHub App) creates
the do-not-merge candidate PR and dispatches the reviewed T7 workflow, which builds the image.
"""

import base64
import json
import re
from pathlib import Path

from .candidate_publication import PublicationSelection, prepare_publication, publication_request_id
from .cutover import _get
from .initial_proposal import InitialProposalExecution, _admission_key
from .store import EventError

_PIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")


def pinned_selections(directory):
    """<publication_request_id>.json per reviewed T7 run; None when there is no pin for a run.

    The pins live under a logical folder, but a ConfigMap mount cannot hold a folder tree. So a
    pin is read from the real tree (<dir>/<id>.json) or from the flat mount of the parent
    (<parent>/<dirname>__<id>.json, a ConfigMap key). A folder that is not mounted at all means
    no pins. Only plain ids are looked up, so a request id can never leave the mount.
    """
    root = Path(directory)
    if not root.is_absolute() or root.name in ("", ".", ".."):
        raise ValueError("publication_selection_mount_required")

    def select(request_id):
        if not isinstance(request_id, str) or _PIN_ID.fullmatch(request_id) is None or ".." in request_id:
            return None
        for path in (root / f"{request_id}.json", root.parent / f"{root.name}__{request_id}.json"):
            if path.is_file():
                return PublicationSelection(**json.loads(path.read_text()))
        return None

    return select


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


def propose_preparer(execution, *, select, repository, github=None, writer=None, auto=None):
    """`select` (operator pin) wins; otherwise `writer` (GitHub App) creates the candidate
    PR, dispatches T7 and derives the selection from the reviewed workflow's run.

    `auto` (initial_proposal_auto.AutoInitialProposal, T49) admits the plan's one registered task and runs
    the AI Job itself; without it an operator does both (operations/initial-proposal/run.py)."""
    if type(execution) is not InitialProposalExecution or not callable(select):
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
        request_id = publication_request_id(intent)
        selection = select(request_id)
        if selection is None and writer is not None:
            selection = writer.selection(value, result, request_id)
        if selection is None:
            raise EventError("initial_publication_unselected")  # T7 run not reviewed yet
        try:
            publication = prepare_publication(value=value, intent=intent, result=result,
                                              selection=selection, repository=repository, github=github)
        except ValueError:
            raise EventError("initial_publication_unconfirmed") from None
        record_apply_input(execution.records.store, plan_id, value, result)
        return execution.evidence_reader(publication=publication)

    return prepare
