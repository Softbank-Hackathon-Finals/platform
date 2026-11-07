"""The plan's run state (T49): the OpenAI Agents SDK `RunState` model applied to the T10 workflow, without the SDK.

`RunState` (openai-agents 0.24 `agents/run_state.py`) is one serializable snapshot of a run, the pending
`interruptions` (decisions someone owes) and approve/reject to resume. The same model here:

* every plan state has exactly one owner that moves it on (`OWNERS`); a state that waits on a decision names
  its gate, and `run_state` lists it as an interruption with who decides (a delegation or a person);
* `check_wiring` refuses a host whose own states have no driver, the guarantee a graph engine gives at
  compile time (LangGraph `compile()`, DBOS workflows): a state is driven or explicitly declared deferred,
  never silently `workflow_reader_unwired`.

No LLM client is imported (design 8.3: the workflow and decision modules stay LLM-free).
"""

import hashlib
import json

from .store import EventError
from .workflow import STAGES, Analysis

# state -> (owner, gate a decision is owed on in this state, or None)
#   host:          the workflow host (WorkflowRecovery via a reader or preparer)
#   approval-host: the T10 operator process (Executor command token, Edge journal): DelegatedGates or a person
#   collection:    the T16 intake (S1; an operator tool until the platform AWS identity, #740)
#   person:        nothing moves it by code (fail closed)
OWNERS = {
    "COLLECT": ("collection", None),
    "ANALYZE": ("host", None),
    "PROPOSE": ("host", None),
    "REHEARSE": ("host", "change"),
    "GATE": ("host", None),
    "AWAIT_APPROVAL": ("host", "shift"),
    "SHIFT": ("approval-host", "cutover"),
    "CUTOVER": ("host", None),
    "WATCH": ("host", "failback"),
    "FAILBACK": ("approval-host", None),
    "SAFE_HOLD": ("person", None),
}
TERMINAL = frozenset({"DONE", "DONE_ROLLED_BACK"})
assert {s for s, (o, _) in OWNERS.items() if o == "host"} == set(STAGES), "every STAGES state is host-owned"


def check_wiring(readers, preparers, *, deferred=()):
    """Refuse to start when a host-owned state has no reader or preparer, unless the deployment declares it
    deferred (a visible, named gap instead of a silent hold)."""
    deferred = set(deferred)
    if deferred - set(STAGES):
        raise ValueError("workflow_deferred_unknown_state")
    missing = sorted(set(STAGES) - set(readers) - set(preparers) - deferred)
    if missing:
        raise ValueError("workflow_state_unwired:" + ",".join(missing))
    return sorted(deferred)


def run_state(c, plan, *, delegation=None):
    """A RunState-like, JSON-serializable snapshot of one plan: where it is, who moves it, what is owed.

    `delegation` is the plan's live delegation receipt (approval_delegation) or None."""
    owner, gate = OWNERS.get(plan["state"], (None, None))
    interruptions = []
    if gate is not None:
        delegated = bool(delegation) and gate in delegation.get("gates", [])
        interruptions.append({"gate": gate, "decides": "delegation" if delegated else "person",
                              "approver": delegation["approver"] if delegated else None})
    return {"plan_id": plan["plan_id"], "state": plan["state"], "version": plan["version"],
            "owner": owner, "terminal": plan["state"] in TERMINAL, "interruptions": interruptions}


def analysis_reader(c, plan, previous, run):
    """ANALYZE -> PROPOSE from the S1 records only: the T16 collection the plan was completed with and the
    first T19 report of its initial GATE context (a BLOCK is the expected S1 outcome). Replaces the operator's
    one-off acceptance; a client never supplies this evidence."""
    from .gate_records import _current
    from .gate_records import _get as gate_get
    from .workflow_stages import _evidence

    plan_id, spec = plan["plan_id"], json.loads(plan["spec_json"])
    rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, "collection-original:%")).fetchall()
    if len(rows) != 1:
        raise EventError("analysis_collection_unavailable")
    original = json.loads(rows[0]["receipt_json"])
    if original.get("collection_scope_digest") != spec["collection_scope_digest"]:
        raise EventError("analysis_collection_unavailable")
    current = _current(c, plan_id)
    if current is None or "gate_phase" in current:  # the initial context only, before any application
        raise EventError("analysis_gate_unavailable")
    report = gate_get(c, plan_id, "security-report", current["context"]["gate_id"])
    if report is None:
        raise EventError("analysis_gate_unavailable")
    comparison = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode()).hexdigest()
    inventory = original["inventory"]["digest"].removeprefix("sha256:")
    return _evidence(plan, "t14-analysis-" + comparison[:32],
                     Analysis(spec["collection_scope_digest"], inventory, comparison))
