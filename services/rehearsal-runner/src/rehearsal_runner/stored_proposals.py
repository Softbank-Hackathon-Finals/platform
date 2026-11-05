"""Consume T10's stored initial proposal (T10 #366 `StoredInitialProposalSource`) as a planner `ProposalSource`.

The model ran once at PROPOSE; REHEARSE never calls it again. The host supplies the same
`ProductInput` the T38 path uses (committed Admission, registered source) and T10's stored result
for this exact first Admission. This module checks that both describe the admitted task, stages the
source through T38's own input checks, and replays the stored proposal through a local T11
Coordinator so T11 policy is enforced again here. Tool events stay with the PROPOSE run and are
reported UNAVAILABLE. Later attempts need their own proposal path and stop before any effect.
"""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class _StoredProvider:
    result: dict = field(repr=False)
    tool_events_available = False  # the model's events belong to the PROPOSE execution

    async def propose(self, bundle, tools) -> None:
        if tools.attempt != 1:
            raise ValueError("the stored initial proposal covers only the first attempt")
        tools.dispatch("mcp__bravo__plan_propose", self.result["proposal"])


@dataclass(frozen=True)
class StoredProposalSource:
    """`ProposalSource.open` backed by T10's stored result. Every port is supplied by the T10 host.

    product_input(admission, task) -> the committed `ProductInput` (same port as `RemoteProposalSource`)
    stored(admission) -> `StoredInitialProposalSource(execution, admission).result()`
    """

    product_input: Callable[[object, object], object] = field(repr=False)
    stored: Callable[[object], dict] = field(repr=False)
    staging: Path = Path(tempfile.gettempdir())

    def open(self, admission: object, task: object) -> tuple[object, object]:
        from bravo_agent.contracts import digest
        from bravo_agent.coordinator import Coordinator
        from bravo_agent.subscription_product import stage_input

        if getattr(admission, "attempt", None) != 1:
            raise ValueError("the stored initial proposal covers only the first attempt")
        value = self.product_input(admission, task)
        if (value.attempt != 1 or value.bundle.run_id != admission.rehearsal_id
                or value.bundle.run_id != task.run_id or value.bundle.source_sha != task.source_sha):
            raise ValueError("product input does not match the admitted task")
        result = self.stored(admission)
        proposal = result.get("proposal") if isinstance(result, dict) else None
        if (not isinstance(proposal, dict) or result.get("state") != "PROPOSAL_READY" or result.get("attempt") != 1
                or result.get("run_id") != admission.rehearsal_id or result.get("source_sha") != task.source_sha
                or proposal.get("run_id") != admission.rehearsal_id or proposal.get("source_sha") != task.source_sha
                or result.get("proposal_digest") != digest(proposal)):
            raise ValueError("stored proposal is not bound to this admitted task")
        root = Path(tempfile.mkdtemp(prefix="t15-stored-", dir=self.staging))
        snapshot = stage_input(value, root)  # T38's input contract: manifest paths and hashes
        return Coordinator(value.bundle, snapshot), _StoredProvider(dict(result))
