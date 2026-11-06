"""T11 side of T42 rehearsal.exec v1: model-facing schemas, argument checks, the channel envelope
and response checks. No channel is implemented here; T38/T15 own the transport and execution.

Contract: docs/contracts/T42-rehearsal-exec-v1-proposal.md and its schema file. The fragments
below mirror that file; ``tests/test_exec_tools.py`` reads it and pins its SHA-256.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from .contracts import digest

EXEC_SCHEMA_VERSION = 1
CONTRACT_SHA256 = "13d8873d57057faf8e0df7448c12fa7cfd0aaeada87af03db138dfae7dc50e06"

# Claude MCP name -> T11 internal name (also the audit ``tool`` and the envelope ``tool``).
EXEC_TOOLS = {
    "mcp__bravo__rehearsal_exec_apply": "rehearsal.exec.apply",
    "mcp__bravo__rehearsal_exec_inspect": "rehearsal.exec.inspect",
    "mcp__bravo__rehearsal_exec_probe": "rehearsal.exec.probe",
}
# OpenAI function name -> canonical MCP name.
EXEC_FUNCTIONS = {name.removeprefix("mcp__bravo__"): name for name in EXEC_TOOLS}
EXEC_ACTIONS = {name: internal.rsplit(".", 1)[1] for name, internal in EXEC_TOOLS.items()}

RESULT_CODES = ("OK", "REJECTED", "FAILED", "UNKNOWN")
REASONS = ("BLOCKED", "FOREIGN_ATTEMPT", "TIMEOUT", "NONZERO_EXIT", "UNAVAILABLE", "EFFECT_UNKNOWN",
           "OUTPUT_WITHHELD", "BUDGET_EXHAUSTED")
STAGES = ("ADMIT", "VALIDATE", "EXECUTE", "OUTPUT")
PROBE_OUTCOMES = ("CONNECTED", "REFUSED", "TIMED_OUT", "DNS_FAILED")
INSPECT_KINDS = ("pod", "deployment", "events", "logs")
PROBE_METHODS = ("TCP", "HTTP_GET", "HTTP_HEAD")
POD_PHASES = ("Pending", "Running", "Succeeded", "Failed", "Unknown")
TARGET_KINDS = ("deployment", "pod", "service")

# Kubernetes DNS label: no leading/trailing hyphen, so a name is never read as a kubectl option.
K8S_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
HTTP_PATH = r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,255}$"
REQUEST_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$"
SHA256 = r"^[a-f0-9]{64}$"


def _full(pattern: str) -> re.Pattern:
    # JSON Schema ``$`` does not match before a trailing newline; Python's does. Use fullmatch.
    return re.compile(pattern.removeprefix("^").removesuffix("$"))


_K8S, _PATH, _REQUEST_ID, _SHA = map(_full, (K8S_NAME, HTTP_PATH, REQUEST_ID, SHA256))

# Model-facing parameters, identical to ``$defs.inspect_args`` / ``$defs.probe_args`` without
# their description. Field-relation rules are not sent (strict mode takes no conditionals).
INSPECT_PARAMETERS = {
    "type": "object", "additionalProperties": False, "required": ["target_kind", "target_name", "lines"],
    "properties": {
        "target_kind": {"type": "string", "enum": list(INSPECT_KINDS)},
        "target_name": {"type": ["string", "null"], "pattern": K8S_NAME},
        "lines": {"type": ["integer", "null"], "minimum": 1, "maximum": 200},
    },
}
PROBE_PARAMETERS = {
    "type": "object", "additionalProperties": False, "required": ["service", "port", "method", "path"],
    "properties": {
        "service": {"type": "string", "pattern": K8S_NAME},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "method": {"type": "string", "enum": list(PROBE_METHODS)},
        "path": {"type": ["string", "null"], "pattern": HTTP_PATH},
    },
}
DESCRIPTIONS = {
    "apply": "Deploy or replace the candidate in the isolated rehearsal space with a full draft proposal "
             "(same schema as plan_propose). Not your submission; submit separately with plan_propose.",
    "inspect": "Read status, events or logs of a candidate object in the rehearsal space. "
               "target_name null only for events; lines only for logs.",
    "probe": "Test a connection to a service inside the rehearsal space. path null for TCP, required for HTTP. "
             "Refused or denied connections are test results.",
}


class ExecChannel(Protocol):
    """Exec-only local channel (T38 broker -> T15). ``enabled`` (channel open, T15 session ready)
    must be exactly ``True``, in addition to the run's ``exec_enabled is True``, for T11 to expose
    the tools; ``send`` returns the contract ``response`` object or raises."""

    enabled: bool

    def send(self, envelope: dict) -> dict: ...


@dataclass(frozen=True)
class ExecReserve:
    """Model-run budget kept for plan.propose. Exec tools close when, after the current call, fewer
    than ``turns`` model calls or ``seconds`` remain. 45 s = one 15 s exec action + one model turn."""

    turns: int = 2
    seconds: float = 45.0


def _integer(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _optional_integer(value, low: int, high: int | None = None) -> bool:
    return value is None or (type(value) is int and value >= low and (high is None or value <= high))


def inspect_arguments(args) -> dict:
    """``inspect_args`` plus ``inspect_rules``."""
    if not isinstance(args, dict) or set(args) != {"target_kind", "target_name", "lines"}:
        raise ValueError("inspect arguments")
    kind, name, lines = args["target_kind"], args["target_name"], args["lines"]
    if (kind not in INSPECT_KINDS or (name is not None and not (type(name) is str and _K8S.fullmatch(name)))
            or not _optional_integer(lines, 1, 200)):
        raise ValueError("inspect arguments")
    if (lines is not None) != (kind == "logs") or (name is None and kind != "events"):
        raise ValueError("inspect field relation")
    return {"target_kind": kind, "target_name": name, "lines": lines}


def probe_arguments(args) -> dict:
    """``probe_args`` plus ``probe_rules``."""
    if not isinstance(args, dict) or set(args) != {"service", "port", "method", "path"}:
        raise ValueError("probe arguments")
    service, port, method, path = args["service"], args["port"], args["method"], args["path"]
    if (not (type(service) is str and _K8S.fullmatch(service)) or not _integer(port, 1, 65535)
            or method not in PROBE_METHODS
            or (path is not None and not (type(path) is str and _PATH.fullmatch(path)))):
        raise ValueError("probe arguments")
    if (path is None) != (method == "TCP"):
        raise ValueError("probe field relation")
    return {"service": service, "port": port, "method": method, "path": path}


def exec_envelope(tool: str, arguments: dict) -> dict:
    """``local_request``: a new request_id per model call, ``args_sha256`` = T11 ``digest``."""
    return {"schema_version": EXEC_SCHEMA_VERSION, "request_id": "t11-" + uuid.uuid4().hex,
            "args_sha256": digest(arguments), "tool": EXEC_TOOLS[tool], "arguments": arguments}


def local_response(envelope: dict, result_code: str, reason: str, stage: str) -> dict:
    """A response T11 builds itself (channel failure, closed exec). Exec is over for the model in
    every such case, so the budget it reports is zero."""
    return {"schema_version": EXEC_SCHEMA_VERSION, "request_id": envelope["request_id"],
            "action": envelope["tool"].rsplit(".", 1)[1], "result_code": result_code, "reason": reason,
            "stage": stage, "status": None, "output_tail": None, "output_truncated": False,
            "budget": {"calls_left": 0, "seconds_left": 0}}


RESPONSE_KEYS = frozenset({"schema_version", "request_id", "action", "result_code", "reason", "stage",
                           "status", "output_tail", "output_truncated", "budget"})
_REASONS_BY_CODE = {
    "OK": {None, "OUTPUT_WITHHELD"},
    "REJECTED": {"BLOCKED", "FOREIGN_ATTEMPT", "EFFECT_UNKNOWN", "BUDGET_EXHAUSTED", "UNAVAILABLE"},
    "FAILED": {"TIMEOUT", "NONZERO_EXIT", "UNAVAILABLE"},
    "UNKNOWN": {"EFFECT_UNKNOWN"},
}


def _status(action: str, status: dict) -> bool:
    if action == "apply":
        targets = status.get("targets")
        return (set(status) == {"kind", "proposal_digest", "targets"} and status["kind"] == "apply"
                and type(status["proposal_digest"]) is str and bool(_SHA.fullmatch(status["proposal_digest"]))
                and isinstance(targets, list) and len(targets) <= 8
                and all(isinstance(t, dict) and set(t) == {"kind", "name"} and t["kind"] in TARGET_KINDS
                        and type(t["name"]) is str and _K8S.fullmatch(t["name"]) for t in targets))
    if action == "inspect":
        return (set(status) == {"kind", "found", "phase", "ready", "replicas", "ready_replicas", "restarts", "events"}
                and status["kind"] == "inspect" and type(status["found"]) is bool
                and (status["phase"] is None or status["phase"] in POD_PHASES)
                and (status["ready"] is None or type(status["ready"]) is bool)
                and all(_optional_integer(status[key], 0) for key in ("replicas", "ready_replicas", "restarts", "events")))
    return (set(status) == {"kind", "probe_outcome", "http_status", "elapsed_ms"} and status["kind"] == "probe"
            and status["probe_outcome"] in PROBE_OUTCOMES and _optional_integer(status["http_status"], 100, 599)
            and _integer(status["elapsed_ms"], 0, 15000))


def check_response(response, envelope: dict) -> dict:
    """The schema ``response`` plus T11's binding checks: same request_id and action, and an apply
    status names the proposal that was sent. Raises ``ValueError``; never echoes the response."""
    action = envelope["tool"].rsplit(".", 1)[1]
    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        raise ValueError("response fields")
    code, reason, stage, status = (response[key] for key in ("result_code", "reason", "stage", "status"))
    tail, budget = response["output_tail"], response["budget"]
    valid = (type(response["schema_version"]) is int and response["schema_version"] == EXEC_SCHEMA_VERSION
             and response["request_id"] == envelope["request_id"] and response["action"] == action
             and code in _REASONS_BY_CODE and reason in _REASONS_BY_CODE[code]
             and (stage is None) == (reason is None) and (stage is None or stage in STAGES)
             and (tail is None or (type(tail) is str and len(tail.encode("utf-8")) <= 2048))
             and type(response["output_truncated"]) is bool
             and isinstance(budget, dict) and set(budget) == {"calls_left", "seconds_left"}
             and _integer(budget["calls_left"], 0, 5) and _integer(budget["seconds_left"], 0, 90)
             and (status is None if code != "OK" else isinstance(status, dict) and _status(action, status))
             and (tail is None or code not in ("REJECTED", "UNKNOWN"))
             and (reason != "OUTPUT_WITHHELD" or (tail is None and response["output_truncated"] is False)))
    if valid and action == "apply" and status is not None:
        valid = status["proposal_digest"] == envelope["args_sha256"]
    if not valid:
        raise ValueError("response outside contract")
    return response
