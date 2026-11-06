"""Action ① (apply): a draft in the submission schema, checked exactly like plan.propose.

The draft is validated with T11's own `Proposal` model against the T11 snapshot
the model reads (run, source revision, editable paths, base digests), so what
the model tries in space A is the same kind of change it will submit. Placing
the change in space A is the task kind's `deploy` function, which must use the
same apply code and runner template as space B grading (ADR-0024 3-3); this
module never builds a Pod spec from model input.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .exec_session import Outcome, Space

_K8S_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class Deployed:
    """What the task kind placed in space A: names from its template, never from the draft."""

    targets: tuple[tuple[str, str], ...]  # (kind, name), kind in deployment/pod/service
    output: bytes = b""

    def __post_init__(self):
        if (len(self.targets) > 8 or any(
                kind not in ("deployment", "pod", "service") or not isinstance(name, str)
                or _K8S_NAME.fullmatch(name) is None for kind, name in self.targets)):
            raise ValueError("invalid deployed targets")


def t11_proposal_check(coordinator) -> Callable[[dict], str]:
    """Return a check that applies plan.propose's validation and returns the proposal digest."""
    from bravo_agent.contracts import Proposal, digest

    def check(arguments: dict) -> str:
        proposal = Proposal.model_validate(arguments)
        bundle, snapshot = coordinator.bundle, coordinator.snapshot
        if proposal.run_id != bundle.run_id or proposal.source_sha != bundle.source_sha:
            raise ValueError("proposal context mismatch")
        for edit in proposal.edits:
            if edit.path not in snapshot.manifest.editable_paths:
                raise ValueError("edit path not allowed")
            snapshot.read(edit.path)
            if edit.before_sha256 != snapshot.manifest.files[edit.path]:
                raise ValueError("base digest mismatch")
        return digest(proposal)

    return check


@dataclass(frozen=True)
class ProposalApply:
    """`exec_session.Action` for apply. `deploy` raises the session's NotSent/Transient/ActionFailed."""

    space: Space
    check: Callable[[dict], str] = field(repr=False)
    deploy: Callable[[dict, Space, float], Deployed] = field(repr=False)

    def validate(self, arguments: dict) -> dict:
        return {"proposal_digest": self.check(arguments)}

    def run(self, arguments: dict, deadline: float) -> Outcome:
        proposal_digest = self.check(arguments)
        deployed = self.deploy(arguments, self.space, deadline)
        if not isinstance(deployed, Deployed):
            raise TypeError("deploy must return Deployed")
        status = {"kind": "apply", "proposal_digest": proposal_digest,
                  "targets": [{"kind": kind, "name": name} for kind, name in deployed.targets]}
        return Outcome(status, deployed.output, {"proposal_digest": proposal_digest})
