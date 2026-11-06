"""Deterministic retry/receipt rules; this module does not import an LLM client."""

import inspect
import json
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from .contracts import FailureBundle, InventorySummary, Proposal, VerificationReceipt, digest
from .tools import Snapshot, Tools

MAX_ATTEMPTS = 3
STATE_SCHEMA_VERSION = 1
STATE_KIND = "bravo.t11.coordinator_state"
STATES = frozenset({"READY", "ANALYZING", "PROPOSAL_READY", "RETRY_ALLOWED", "AWAITING_APPROVAL", "NEEDS_HUMAN"})
# Exactly what a fresh process needs to continue the same run. ``state_digest`` covers the rest.
STATE_FIELDS = frozenset({
    "schema_version", "kind", "run_id", "source_sha", "snapshot_digest", "inventory_digest",
    "revision", "attempt", "state", "task", "bundle", "proposal", "proposal_digest",
    "attempts", "audit", "state_digest",
})
ATTEMPT_FIELDS = frozenset({
    "attempt", "input", "input_digest", "task_digest", "state", "model_usage", "audit_event_count",
    "audit_truncated", "proposal", "proposal_digest",
})
# Bounds a stored document, measured as UTF-8 (not ``\uXXXX`` escapes). Worst case on the normal
# path is three proposals of four 32,768-character edits that JSON still escapes (6 bytes each),
# about 2.4 MB, plus three attempts of 128 events and verifier/restart events.
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_AUDIT_EVENTS = 3 * 129 + 8


class Provider(Protocol):
    async def propose(self, bundle: FailureBundle, tools: Tools) -> None: ...


class StateRejected(ValueError):
    """A persisted coordinator state that must not be continued automatically."""


def _decide(receipt: VerificationReceipt, task: FailureBundle, proposal_digest: str, attempt: int) -> tuple[str, bool]:
    """The ``verify()`` rule; ``restore()`` re-derives every stored receipt's decision with it."""
    matches = (receipt.run_id == task.run_id
               and receipt.source_sha == task.source_sha
               and receipt.proposal_digest == proposal_digest
               and receipt.attempt == attempt)
    if receipt.failure is not None:
        matches = (matches and receipt.failure.run_id == task.run_id
                   and receipt.failure.source_sha == task.source_sha
                   and receipt.failure.evidence_digest == receipt.evidence_digest)
    if not matches or receipt.verdict == "UNKNOWN" or not receipt.effects_known:
        return "NEEDS_HUMAN", matches
    if receipt.verdict == "PASS":
        return "AWAITING_APPROVAL", matches
    return ("NEEDS_HUMAN" if attempt >= MAX_ATTEMPTS else "RETRY_ALLOWED"), matches


class Coordinator:
    def __init__(self, bundle: FailureBundle, snapshot: Snapshot, inventory: InventorySummary | None = None,
                 checkpoint: Callable[[dict], None] | None = None):
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
        # Write-ahead persistence for T10: called with ``export_state()`` after every transition,
        # including ANALYZING *before* the model is called.
        self.checkpoint = checkpoint
        self.revision = 0

    def _transition(self) -> None:
        self.revision += 1
        if self.checkpoint is not None:
            result = self.checkpoint(self.export_state())
            # An async store would only create a coroutine here and the model would run with
            # nothing written. Refuse it so the transition stops as STATE_CHECKPOINT_FAILED.
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("checkpoint must store synchronously")

    async def propose(self, provider: Provider) -> dict:
        if self.state not in {"READY", "RETRY_ALLOWED"} or self.attempt >= MAX_ATTEMPTS:
            raise ValueError("proposal not allowed in this state")
        self.attempt += 1
        tools = Tools(self.bundle, self.snapshot, self.attempt, inventory=self.inventory, task=self.task)
        self.proposal = None
        self.state = "ANALYZING"
        try:
            # The attempt must be durable before the model can spend it; otherwise stop here.
            self._transition()
        except Exception:
            # Not called: the attempt is unused, but this process does not continue the run.
            self.state = "NEEDS_HUMAN"
            self._finish_attempt(tools)
            self._stop("STATE_CHECKPOINT_FAILED")
            return self.result()
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
            self._finish_attempt(tools)
        self._checkpoint_or_stop()
        return self.result()

    def _finish_attempt(self, tools: Tools) -> None:
        self.audit.extend(tools.events)
        self.attempts.append({"attempt": self.attempt, "input": self.bundle.model_dump(),
                              "input_digest": digest(self.bundle), "task_digest": digest(self.task),
                              "state": self.state,
                              "model_usage": dict(tools.usage),
                              "audit_event_count": tools.event_count,
                              "audit_truncated": tools.event_count > len(tools.events),
                              "proposal": self.proposal.model_dump() if self.proposal else None,
                              "proposal_digest": digest(self.proposal) if self.proposal else None})

    def _checkpoint_or_stop(self) -> None:
        """A transition that could not be stored is not continued: the stored copy is older and
        restoring it must not reach a different decision than this process."""
        try:
            self._transition()
        except Exception:
            self.state = "NEEDS_HUMAN"
            self._stop("STATE_CHECKPOINT_FAILED")

    def _stop(self, reason: str) -> None:
        self.audit.append({"run_id": self.bundle.run_id, "attempt": self.attempt, "tool": "coordinator",
                           "outcome": "STOPPED", "reason": reason})
        # Every stop follows its attempt's record. Counting the event there keeps "this attempt's
        # non-verifier events == audit_event_count" true for T15's attempt_tool_events.
        self.attempts[-1]["audit_event_count"] += 1

    def verify(self, receipt: VerificationReceipt) -> str:
        if self.state != "PROPOSAL_READY" or self.proposal is None:
            raise ValueError("no proposal awaiting independent verification")
        self.state, matches = _decide(receipt, self.task, digest(self.proposal), self.attempt)
        if self.state == "RETRY_ALLOWED":
            self.bundle = receipt.failure
        if matches:
            # A receipt for another proposal/attempt/revision is not this attempt's result: only the
            # RECEIPT_MISMATCH reason below is kept, so a stored state never carries a foreign receipt.
            self.attempts[-1]["verification"] = receipt.model_dump()
        self.audit.append({"run_id": self.bundle.run_id, "attempt": self.attempt,
                           "tool": "independent_verifier", "outcome": self.state,
                           "reason": "RECEIPT_ACCEPTED" if matches else "RECEIPT_MISMATCH",
                           "evidence_digest": receipt.evidence_digest})
        self._checkpoint_or_stop()
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

    def export_state(self) -> dict:
        """JSON-serialisable state for T10 to persist. ``state_digest`` detects corruption or a
        partial edit; it is not a signature. Authenticity and single-writer ordering (compare on
        ``revision``) belong to the store."""
        body = {"schema_version": STATE_SCHEMA_VERSION, "kind": STATE_KIND,
                "run_id": self.task.run_id, "source_sha": self.task.source_sha,
                "snapshot_digest": digest(self.snapshot.manifest),
                "inventory_digest": digest(self.inventory) if self.inventory is not None else None,
                "revision": self.revision, "attempt": self.attempt, "state": self.state,
                "task": self.task.model_dump(mode="json"), "bundle": self.bundle.model_dump(mode="json"),
                "proposal": self.proposal.model_dump(mode="json") if self.proposal else None,
                "proposal_digest": digest(self.proposal) if self.proposal else None,
                "attempts": self.attempts, "audit": self.audit}
        # Detach from live lists so a stored copy cannot be mutated through the coordinator.
        body = json.loads(json.dumps(body))
        return body | {"state_digest": digest(body)}

    @classmethod
    def restore(cls, state: dict, snapshot: Snapshot, inventory: InventorySummary | None = None,
                checkpoint: Callable[[dict], None] | None = None) -> "Coordinator":
        """Continue a run in a fresh process. Raises ``StateRejected`` for any state that does not
        check out; the caller then hands the run to a human instead of starting over."""
        try:
            state = _checked(state, snapshot, inventory)
            task = FailureBundle.model_validate(state["task"])
            coordinator = cls(task, snapshot, inventory, checkpoint=None)
            coordinator.bundle = FailureBundle.model_validate(state["bundle"])
            coordinator.proposal = (Proposal.model_validate(state["proposal"])
                                    if state["proposal"] is not None else None)
            coordinator.attempt, coordinator.state = state["attempt"], state["state"]
            coordinator.attempts, coordinator.audit = state["attempts"], state["audit"]
            coordinator.revision = state["revision"]
        except (ValueError, TypeError, KeyError, RecursionError, ValidationError) as error:
            if isinstance(error, StateRejected):
                raise
            raise StateRejected("coordinator state rejected") from None
        coordinator.checkpoint = checkpoint
        if coordinator.state == "ANALYZING":
            # A model call was in flight when the state was stored. Its outcome is unknown, so the
            # attempt is spent and never re-run automatically.
            coordinator.state = "NEEDS_HUMAN"
            coordinator.attempts.append({"attempt": coordinator.attempt, "input": coordinator.bundle.model_dump(),
                                         "input_digest": digest(coordinator.bundle),
                                         "task_digest": digest(coordinator.task), "state": "NEEDS_HUMAN",
                                         "model_usage": {}, "audit_event_count": 0, "audit_truncated": True,
                                         "proposal": None, "proposal_digest": None})
            coordinator._stop("RESTART_OUTCOME_UNKNOWN")
            coordinator._checkpoint_or_stop()
        return coordinator


def _checked(state, snapshot: Snapshot, inventory: InventorySummary | None) -> dict:
    """Structural and cross-field checks; every failure is the same ``StateRejected``."""
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise StateRejected("unexpected state fields")
    text = json.dumps(state, allow_nan=False)
    size = len(json.dumps(state, allow_nan=False, ensure_ascii=False).encode("utf-8", "surrogatepass"))
    if size > MAX_STATE_BYTES:
        raise StateRejected("state too large")
    state = json.loads(text)  # detached copy: the restored coordinator never aliases caller data
    body = {key: value for key, value in state.items() if key != "state_digest"}
    if state["state_digest"] != digest(body):
        raise StateRejected("state digest mismatch")
    if state["schema_version"] != STATE_SCHEMA_VERSION or state["kind"] != STATE_KIND:
        raise StateRejected("unknown state version")
    attempt, current, revision = state["attempt"], state["state"], state["revision"]
    if (type(attempt) is not int or not 0 <= attempt <= MAX_ATTEMPTS or current not in STATES
            or type(revision) is not int or revision < 0):
        raise StateRejected("attempt or state outside policy")
    task = FailureBundle.model_validate(state["task"])
    bundle = FailureBundle.model_validate(state["bundle"])
    if (state["run_id"] != task.run_id or state["source_sha"] != task.source_sha
            or bundle.run_id != task.run_id or bundle.source_sha != task.source_sha):
        raise StateRejected("run binding mismatch")
    if (state["snapshot_digest"] != digest(snapshot.manifest)
            or state["inventory_digest"] != (digest(inventory) if inventory is not None else None)):
        raise StateRejected("snapshot or inventory binding mismatch")
    proposal = Proposal.model_validate(state["proposal"]) if state["proposal"] is not None else None
    if (state["proposal_digest"] != (digest(proposal) if proposal else None)
            or (proposal and (proposal.run_id, proposal.source_sha) != (task.run_id, task.source_sha))):
        raise StateRejected("proposal binding mismatch")

    attempts, audit = state["attempts"], state["audit"]
    if not isinstance(attempts, list) or not isinstance(audit, list) or len(audit) > MAX_AUDIT_EVENTS:
        raise StateRejected("attempt records outside policy")
    # ANALYZING means the record for the running attempt was not written yet.
    if len(attempts) != (attempt - 1 if current == "ANALYZING" else attempt):
        raise StateRejected("attempt count mismatch")
    # Every record is re-judged with the verify() rule: a re-signed document cannot turn a stopped
    # attempt into a retry, carry a foreign receipt, or swap the input an attempt was given.
    expected_input, decision = task, None
    for index, record in enumerate(attempts, start=1):
        if (not isinstance(record, dict) or not ATTEMPT_FIELDS <= set(record)
                or not set(record) <= ATTEMPT_FIELDS | {"verification"}
                or type(record["attempt"]) is not int or record["attempt"] != index):
            raise StateRejected("attempt record mismatch")
        given = FailureBundle.model_validate(record["input"])
        offered = Proposal.model_validate(record["proposal"]) if record["proposal"] is not None else None
        if (record["task_digest"] != digest(task) or record["input_digest"] != digest(given)
                or digest(given) != digest(expected_input)
                or record["proposal_digest"] != (digest(offered) if offered else None)):
            raise StateRejected("attempt record mismatch")
        decision = None
        if "verification" in record:
            receipt = VerificationReceipt.model_validate(record["verification"])
            if offered is None or record["state"] != "PROPOSAL_READY":
                raise StateRejected("receipt without a proposal")
            decision, matches = _decide(receipt, task, record["proposal_digest"], index)
            if not matches:
                raise StateRejected("receipt binding mismatch")
            if decision == "RETRY_ALLOWED":
                expected_input = receipt.failure
        # Only a verified FAIL with known effects opens the next attempt.
        if (index < len(attempts) or current == "ANALYZING") and decision != "RETRY_ALLOWED":
            raise StateRejected("earlier attempt did not end in a retry")
    for event in audit:
        if (not isinstance(event, dict) or event.get("run_id") != task.run_id
                or type(event.get("attempt")) is not int or not 1 <= event["attempt"] <= max(attempt, 1)):
            raise StateRejected("audit event outside this run")

    last = attempts[-1] if attempts else None
    given = digest(bundle)
    shown = last is not None and last["proposal_digest"] == state["proposal_digest"]
    expected = {
        "READY": attempt == 0 and proposal is None and given == digest(task),
        "ANALYZING": attempt >= 1 and proposal is None and given == digest(expected_input),
        "PROPOSAL_READY": (shown and proposal is not None and "verification" not in last
                           and last["state"] == "PROPOSAL_READY" and given == last["input_digest"]),
        "RETRY_ALLOWED": (shown and proposal is not None and decision == "RETRY_ALLOWED"
                          and given == digest(expected_input)),
        "AWAITING_APPROVAL": (shown and proposal is not None and decision == "AWAITING_APPROVAL"
                              and given == last["input_digest"]),
        # Terminal: any recorded decision may end here (a mismatched receipt or a failed checkpoint).
        "NEEDS_HUMAN": (last is not None and (proposal is None or shown)
                        and given in {last["input_digest"], digest(expected_input)}),
    }[current]
    if not expected:
        raise StateRejected("state does not match its records")
    return state
