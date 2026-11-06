"""PROPOSE for the workflow host: the T38 Job result plus a reviewed T7 publication.

Out of the plan lock: read the saved input/intent/result (current grant rechecked),
take the operator-pinned PublicationSelection for that request and authenticate the
existing T7 run with GitHub GETs. The returned reader only checks locally; the
existing evidence_reader rechecks authority under the lock. No dispatch, no build.
"""

import json
from pathlib import Path

from .candidate_publication import PublicationSelection, prepare_publication, publication_request_id
from .cutover import _get
from .initial_proposal import InitialProposalExecution, _slot
from .store import EventError


def pinned_selections(directory):
    """Protected mount: <publication_request_id>.json per reviewed T7 run. None when absent."""
    root = Path(directory)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("publication_selection_mount_required")

    def select(request_id):
        path = root / f"{request_id}.json"
        if not path.is_file():
            return None
        return PublicationSelection(**json.loads(path.read_text()))

    return select


def propose_preparer(execution, *, select, repository, github=None):
    if type(execution) is not InitialProposalExecution or not callable(select):
        raise ValueError("propose_ports_required")

    def prepare(plan_id, _tenant_id, version):
        with execution.records.store._transaction() as c:
            admitted = _get(c, plan_id, _slot(version))
        if not admitted:
            raise EventError("initial_execution_not_complete")
        value, intent, result = execution.publication_inputs(plan_id, version, **admitted[1])
        selection = select(publication_request_id(intent))
        if selection is None:
            raise EventError("initial_publication_unselected")  # T7 run not reviewed yet
        try:
            publication = prepare_publication(value=value, intent=intent, result=result,
                                              selection=selection, repository=repository, github=github)
        except ValueError:
            raise EventError("initial_publication_unconfirmed") from None
        return execution.evidence_reader(publication=publication)

    return prepare
