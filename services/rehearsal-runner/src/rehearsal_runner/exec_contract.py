"""rehearsal.exec call contract v1 as T15 receives it.

The prose rules are docs/contracts/T42-rehearsal-exec-v1-proposal.md and the
machine-checkable original is T42-rehearsal-exec-v1.schema.json next to it.
This module checks only what T15 must not trust from the channel; apply
arguments are validated by the injected applier with T11's Proposal model.
"""

import hashlib
import json
import re
from dataclasses import dataclass

SCHEMA_VERSION = 1
TOOLS = {"rehearsal.exec.apply": "apply", "rehearsal.exec.inspect": "inspect",
         "rehearsal.exec.probe": "probe"}
RESULT_CODES = ("OK", "REJECTED", "FAILED", "UNKNOWN")
REASONS = ("BLOCKED", "FOREIGN_ATTEMPT", "TIMEOUT", "NONZERO_EXIT", "UNAVAILABLE",
           "EFFECT_UNKNOWN", "OUTPUT_WITHHELD", "BUDGET_EXHAUSTED")
STAGES = ("ADMIT", "VALIDATE", "EXECUTE", "OUTPUT")
PROBE_OUTCOMES = ("CONNECTED", "REFUSED", "TIMED_OUT", "DNS_FAILED")

_RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
# DNS label: a leading "-" would otherwise reach kubectl as an option.
_K8S_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_HTTP_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,255}")
_REQUEST_KEYS = {"schema_version", "run_id", "attempt", "request_id", "args_sha256", "tool", "arguments"}


class MalformedRequest(ValueError):
    """No request ID or tool can be trusted; answer as a channel error, not a contract response."""


class Rejected(Exception):
    def __init__(self, reason: str, stage: str):
        super().__init__(reason)
        self.reason, self.stage = reason, stage


@dataclass(frozen=True)
class Request:
    run_id: str
    attempt: int
    request_id: str
    args_sha256: str
    action: str
    arguments: dict


def args_sha256(arguments: dict) -> str:
    """Same encoding as T11 `contracts.digest` over a plain dict."""
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_int(value) -> bool:
    return type(value) is int


def parse_request(raw) -> tuple[Request | None, str, str]:
    """Return (request, request_id, action) or raise MalformedRequest.

    A request whose ID and tool are readable but whose envelope is otherwise
    wrong is returned as (None, request_id, action) so the caller answers
    BLOCKED at ADMIT with a valid response.
    """
    if not isinstance(raw, dict):
        raise MalformedRequest("request is not an object")
    request_id, tool = raw.get("request_id"), raw.get("tool")
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None or tool not in TOOLS:
        raise MalformedRequest("request ID or tool unreadable")
    action = TOOLS[tool]
    run_id, attempt, digest, arguments = (raw.get(k) for k in ("run_id", "attempt", "args_sha256", "arguments"))
    if (set(raw) != _REQUEST_KEYS or raw.get("schema_version") != SCHEMA_VERSION
            or type(raw.get("schema_version")) is not int
            or not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None
            or not _is_int(attempt) or not 1 <= attempt <= 3
            or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            or not isinstance(arguments, dict)):
        return None, request_id, action
    return Request(run_id, attempt, request_id, digest, action, arguments), request_id, action


def _name(value) -> bool:
    return isinstance(value, str) and _K8S_NAME.fullmatch(value) is not None


def inspect_record(arguments: dict) -> dict:
    """Validate inspect arguments (schema plus cross-field rules); return record keys."""
    kind, name, lines = (arguments.get(k) for k in ("target_kind", "target_name", "lines"))
    if (set(arguments) != {"target_kind", "target_name", "lines"}
            or kind not in ("pod", "deployment", "events", "logs")
            or not (name is None or _name(name)) or (name is None and kind != "events")
            or not (lines is None or (_is_int(lines) and 1 <= lines <= 200))
            or (lines is None) != (kind != "logs")):
        raise ValueError("invalid inspect arguments")
    return {"target_kind": kind} | ({"target_name": name} if name is not None else {}) \
        | ({"lines": lines} if lines is not None else {})


def probe_record(arguments: dict) -> dict:
    """Validate probe arguments; return record keys."""
    service, port, method, path = (arguments.get(k) for k in ("service", "port", "method", "path"))
    if (set(arguments) != {"service", "port", "method", "path"} or not _name(service)
            or not _is_int(port) or not 1 <= port <= 65535
            or method not in ("TCP", "HTTP_GET", "HTTP_HEAD")
            or not (path is None or (isinstance(path, str) and _HTTP_PATH.fullmatch(path) is not None))
            or (path is None) != (method == "TCP")):
        raise ValueError("invalid probe arguments")
    return {"service": service, "port": port, "method": method} | ({"path": path} if path is not None else {})


_EVENT_COMMON = {"run_id", "attempt", "seq", "action", "request_id", "args_sha256", "result_code", "reason",
                 "stage", "output_sha256", "output_bytes"}
EVENT_KEYS = {"apply": _EVENT_COMMON | {"proposal_digest"},
              "inspect": _EVENT_COMMON | {"target_kind", "target_name", "lines"},
              "probe": _EVENT_COMMON | {"service", "port", "method", "path", "probe_outcome", "http_status"}}
MAX_OUTPUT_BYTES = 1048576


def _int_in(value, low: int, high: int) -> bool:
    return _is_int(value) and low <= value <= high


def _match(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def valid_exec_event(event) -> bool:
    """Same rules as the schema's exec_event; values are never null."""
    if not isinstance(event, dict):
        return False
    action, code, reason = event.get("action"), event.get("result_code"), event.get("reason")
    if action not in EVENT_KEYS or not set(event) <= EVENT_KEYS[action]:
        return False
    checks = {
        "run_id": lambda v: _match(_RUN_ID, v), "attempt": lambda v: _int_in(v, 1, 3),
        "seq": lambda v: _int_in(v, 1, 32), "request_id": lambda v: _match(_REQUEST_ID, v),
        "args_sha256": lambda v: _match(_SHA256, v), "result_code": lambda v: v in RESULT_CODES,
        "reason": lambda v: v in REASONS, "stage": lambda v: v in STAGES,
        "output_sha256": lambda v: _match(_SHA256, v), "output_bytes": lambda v: _int_in(v, 0, MAX_OUTPUT_BYTES),
        "proposal_digest": lambda v: _match(_SHA256, v),
        "target_kind": lambda v: v in ("pod", "deployment", "events", "logs"),
        "target_name": _name, "lines": lambda v: _int_in(v, 1, 200), "service": _name,
        "port": lambda v: _int_in(v, 1, 65535), "method": lambda v: v in ("TCP", "HTTP_GET", "HTTP_HEAD"),
        "path": lambda v: _match(_HTTP_PATH, v), "probe_outcome": lambda v: v in PROBE_OUTCOMES,
        "http_status": lambda v: _int_in(v, 100, 599),
    }
    if any(key not in event for key in ("run_id", "attempt", "seq", "request_id", "args_sha256", "result_code")):
        return False
    if any(type(v) not in (str, int, bool) or not checks[k](v) for k, v in event.items() if k != "action"):
        return False
    if ("reason" in event) != ("stage" in event) or ("output_sha256" in event) != ("output_bytes" in event):
        return False
    return (code == "OK") == (reason in (None, "OUTPUT_WITHHELD"))
