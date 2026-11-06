"""Deterministic retry/receipt rules; this module does not import an LLM client."""

from typing import Protocol

from .contracts import FailureBundle, InventorySummary, Proposal, VerificationReceipt, digest
from .exec_tools import ExecChannel, ExecReserve
from .tools import Snapshot, Tools


class Provider(Protocol):
    async def propose(self, bundle: FailureBundle, tools: Tools) -> None: ...


class Coordinator:
    def __init__(self, bundle: FailureBundle, snapshot: Snapshot, inventory: InventorySummary | None = None,
                 *, exec_channel: ExecChannel | None = None, exec_reserve: ExecReserve | None = None,
                 exec_enabled: bool = False):
        if bundle.source_sha != snapshot.manifest.source_sha:
            raise ValueError("source revision mismatch")
        if inventory is not None and inventory.source_sha != bundle.source_sha:
            raise ValueError("inventory revision mismatch")
        self.bundle, self.snapshot, self.inventory = bundle, snapshot, inventory
        self.task = bundle  # kept across retries; ``bundle`` becomes the verifier's feedback
        self.attempt = 0
        self.state = "READY"
        self.proposal: Proposal | None = None
        self.audit: list[dict] = []
        self.attempts: list[dict] = []
        # T42: handed to every attempt's Tools; exec state (effect unknown, closed) is per attempt.
        # Off by default; only the run's explicit ``exec_enabled is True`` plus a channel opens exec.
        self.exec_channel, self.exec_reserve = exec_channel, exec_reserve
        self.exec_enabled = exec_enabled is True

    async def propose(self, provider: Provider) -> dict:
        if self.state not in {"READY", "RETRY_ALLOWED"} or self.attempt >= 3:
            raise ValueError("proposal not allowed in this state")
        self.attempt += 1
        tools = Tools(self.bundle, self.snapshot, self.attempt, inventory=self.inventory, task=self.task,
                      exec_channel=self.exec_channel, exec_reserve=self.exec_reserve,
                      exec_enabled=self.exec_enabled)
        self.proposal = None
        self.state = "ANALYZING"
        try:
            await provider.propose(self.bundle, tools)
            if tools.proposal is None or tools.event_count > len(tools.events):
                raise ValueError("missing proposal or incomplete audit")
            self.proposal = tools.proposal
            self.state = "PROPOSAL_READY"
        except Exception:
            # Model/transport/schema failures are not proof that repeating is safe.
            self.state = "NEEDS_HUMAN"
            tools.record("provider", "STOPPED", "MODEL_OR_POLICY_FAILURE")
        finally:
            self.audit.extend(tools.events)
            self.attempts.append({"attempt": self.attempt, "input": self.bundle.model_dump(),
                                  "input_digest": digest(self.bundle), "task_digest": digest(self.task),
                                  "state": self.state,
                                  "model_usage": dict(tools.usage),
                                  "audit_event_count": tools.event_count,
                                  "audit_truncated": tools.event_count > len(tools.events),
                                  "proposal": self.proposal.model_dump() if self.proposal else None,
                                  "proposal_digest": digest(self.proposal) if self.proposal else None})
        return self.result()

    def verify(self, receipt: VerificationReceipt) -> str:
        if self.state != "PROPOSAL_READY" or self.proposal is None:
            raise ValueError("no proposal awaiting independent verification")
        matches = (receipt.run_id == self.bundle.run_id
                   and receipt.source_sha == self.bundle.source_sha
                   and receipt.proposal_digest == digest(self.proposal)
                   and receipt.attempt == self.attempt)
        if receipt.failure is not None:
            matches = (matches and receipt.failure.run_id == self.bundle.run_id
                       and receipt.failure.source_sha == self.bundle.source_sha
                       and receipt.failure.evidence_digest == receipt.evidence_digest)
        if not matches or receipt.verdict == "UNKNOWN" or not receipt.effects_known:
            self.state = "NEEDS_HUMAN"
        elif receipt.verdict == "PASS":
            self.state = "AWAITING_APPROVAL"
        elif self.attempt >= 3:
            self.state = "NEEDS_HUMAN"
        else:
            self.state = "RETRY_ALLOWED"
            self.bundle = receipt.failure
        self.attempts[-1]["verification"] = receipt.model_dump()
        self.audit.append({"run_id": self.bundle.run_id, "attempt": self.attempt,
                           "tool": "independent_verifier", "outcome": self.state,
                           "reason": "RECEIPT_ACCEPTED" if matches else "RECEIPT_MISMATCH",
                           "evidence_digest": receipt.evidence_digest})
        return self.state

    def result(self) -> dict:
        # Which T16 inventory was offered, only when one was attached (the key is absent otherwise).
        attached = {"inventory_digest": self.inventory.digest} if self.inventory is not None else {}
        return {**attached, "run_id": self.bundle.run_id, "source_sha": self.bundle.source_sha,
                "attempt": self.attempt, "state": self.state,
                "input_digest": digest(self.bundle), "input": self.bundle.model_dump(),
                "task_digest": digest(self.task), "task": self.task.model_dump(),
                "snapshot_digest": digest(self.snapshot.manifest),
                "proposal": self.proposal.model_dump() if self.proposal else None,
                "proposal_digest": digest(self.proposal) if self.proposal else None,
                "attempts": list(self.attempts),
                "audit": list(self.audit)}
