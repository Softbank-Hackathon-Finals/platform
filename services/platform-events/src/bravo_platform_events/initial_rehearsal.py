"""Read a persisted initial Proposal through the existing T15 consumer port."""

from dataclasses import dataclass, field

from .initial_proposal import InitialProposalExecution
from .rehearsal import Admission


@dataclass(frozen=True)
class StoredInitialProposalSource:
    execution: InitialProposalExecution = field(repr=False)
    admission: Admission

    def result(self):
        return self.execution.rehearsal_result(self.admission)
