"""PROPOSE for the workflow host: the T38 Job result plus a reviewed T7 publication.

Out of the plan lock: read the saved input/intent/result (current grant rechecked),
take the operator-pinned PublicationSelection for that request and authenticate the
existing T7 run with GitHub GETs. The returned reader only checks locally; the
existing evidence_reader rechecks authority under the lock. The host never builds or approves:
with an operator pin it only reads; without one, `writer` (CandidateWriter, GitHub App) creates
the do-not-merge candidate PR and dispatches the reviewed T7 workflow, which builds the image.
"""

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


def propose_preparer(execution, *, select, repository, github=None, writer=None):
    """`select` (operator pin) wins; otherwise `writer` (GitHub App) creates the candidate
    PR, dispatches T7 and derives the selection from the reviewed workflow's run."""
    if type(execution) is not InitialProposalExecution or not callable(select):
        raise ValueError("propose_ports_required")

    def prepare(plan_id, _tenant_id, version):
        with execution.records.store._transaction() as c:
            admitted = _get(c, plan_id, _admission_key(c, plan_id, version))
        if not admitted:
            raise EventError("initial_execution_not_complete")
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
        return execution.evidence_reader(publication=publication)

    return prepare
