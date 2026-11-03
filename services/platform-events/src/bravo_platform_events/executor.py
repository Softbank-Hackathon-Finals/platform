"""T12 transport. Observations never authorize or complete a plan.

ExecutorReader remains GET-only. ExecutorClient adds one POST for the host's
durably claimed command. Neither retries commands or releases leases.
"""

import json
import math
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .contract import validate_identifier

MAX_RESPONSE_BYTES = 262_144
MAX_STEPS = 256
STATUSES = frozenset({"PENDING", "RUNNING", "DONE", "ABORT_PRE_WRITE", "SAFE_HOLD"})
STEP_STATUSES = frozenset({"started", "succeeded", "failed", "unknown"})


def _uuid(value: str) -> None:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid_execution_id")


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_timestamp")
    return parsed


@dataclass(frozen=True)
class ExpectedExecution:
    """Server-owned lookup context. Construction is not authorization proof."""

    plan_id: str
    epoch: int
    execution_id: str | None = None

    def __post_init__(self):
        validate_identifier(self.plan_id)
        if type(self.epoch) is not int or not 1 <= self.epoch <= 2**63 - 1:
            raise ValueError("invalid_epoch")
        if self.execution_id is not None:
            _uuid(self.execution_id)


@dataclass(frozen=True)
class ExecutionStep:
    step: str
    status: str
    elapsed_ms: int | None = None


@dataclass(frozen=True)
class ExecutionSnapshot:
    """Allowlisted remote fields; raw result/detail/connection data are excluded.

    remote_status=DONE is the Executor's report, not the T10 completion verdict.
    created_at/updated_at belong to the whole execution, not individual steps.
    """

    execution_id: str
    plan_id: str
    epoch: int
    remote_status: str
    committed: bool
    steps: tuple[ExecutionStep, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ExecutionRead:
    """UNKNOWN means no usable observation; the host must keep its prior hold."""

    outcome: str
    reason: str | None = None
    snapshot: ExecutionSnapshot | None = None


def _unknown(reason: str) -> ExecutionRead:
    return ExecutionRead("UNKNOWN", reason=reason)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _snapshot(value: dict, expected: ExpectedExecution) -> ExecutionSnapshot:
    if not isinstance(value, dict):
        raise ValueError("invalid_snapshot")
    execution_id = value["execution_id"]
    _uuid(execution_id)
    if (
        value["plan_id"] != expected.plan_id
        or type(value["epoch"]) is not int
        or value["epoch"] != expected.epoch
        or (expected.execution_id is not None and execution_id != expected.execution_id)
    ):
        raise ValueError("identity_mismatch")
    status, committed = value["status"], value["committed"]
    if status not in STATUSES or type(committed) is not bool:
        raise ValueError("invalid_status")
    if (status == "DONE" and not committed) or (status == "ABORT_PRE_WRITE" and committed):
        raise ValueError("inconsistent_commit")
    if _timestamp(value["updated_at"]) < _timestamp(value["created_at"]):
        raise ValueError("invalid_timestamp_order")
    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
        raise ValueError("invalid_steps")
    steps = []
    for step in raw_steps:
        if not isinstance(step, dict):
            raise ValueError("invalid_step")
        validate_identifier(step["step"])
        if step["status"] not in STEP_STATUSES:
            raise ValueError("invalid_step_status")
        elapsed = step.get("elapsed_ms")
        if "elapsed_ms" in step and (type(elapsed) is not int or not 0 <= elapsed <= 2**53 - 1):
            raise ValueError("invalid_elapsed_ms")
        steps.append(ExecutionStep(step["step"], step["status"], elapsed))
    return ExecutionSnapshot(
        execution_id, expected.plan_id, expected.epoch, status, committed,
        tuple(steps), value["created_at"], value["updated_at"],
    )


def _extends(current: ExecutionSnapshot, previous: ExecutionSnapshot) -> bool:
    """Reject stale/rewritten observations; never repair them by inventing state."""
    return (
        (current.execution_id, current.plan_id, current.epoch)
        == (previous.execution_id, previous.plan_id, previous.epoch)
        and current.created_at == previous.created_at
        and _timestamp(current.updated_at) >= _timestamp(previous.updated_at)
        and current.steps[:len(previous.steps)] == previous.steps
        and (not previous.committed or current.committed)
        and (previous.remote_status != "RUNNING" or current.remote_status != "PENDING")
        and (
            previous.remote_status not in {"DONE", "ABORT_PRE_WRITE", "SAFE_HOLD"}
            or current.remote_status == previous.remote_status
        )
    )


class ExecutorReader:
    """One bounded GET against a configured origin, with TLS and no redirects.

    HTTP is permitted only for literal loopback test servers. Production callers
    provide the verified service origin and optional in-memory authentication.
    An HTTP 200 from /healthz is never consulted as execution-readiness evidence.
    """

    def __init__(
        self, base_url: str, *, token: str | None = None, timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        tls_context: ssl.SSLContext | None = None,
    ):
        try:
            url = urlsplit(base_url)
            port = url.port
            valid = (
                url.scheme in {"https", "http"} and url.hostname
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path in {"", "/"} and (port is None or port > 0)
                and (url.scheme == "https" or url.hostname in {"127.0.0.1", "::1"})
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("invalid_executor_origin")
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid_executor_timeout")
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(not 33 <= ord(c) <= 126 for c in token)):
            raise ValueError("invalid_executor_token")
        if tls_context is not None and (not isinstance(tls_context, ssl.SSLContext)
                or tls_context.verify_mode != ssl.CERT_REQUIRED or not tls_context.check_hostname):
            raise ValueError("verified_tls_context_required")
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, follow_redirects=False,
            trust_env=False, transport=transport, verify=tls_context or True,
            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                     **({"Authorization": f"Bearer {token}"} if token else {})},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(
        self, expected: ExpectedExecution, *, previous: ExecutionSnapshot | None = None,
    ) -> ExecutionRead:
        """Read by known ID, or require one exact plan/epoch match after response loss.

        Empty/ambiguous lists and every read failure are UNKNOWN. This method
        never reissues a command, creates an epoch, or releases a lease.
        The host persists the binding/previous snapshot before using this API
        across restarts; this adapter does not claim that persistence exists.
        """
        if not isinstance(expected, ExpectedExecution):
            raise ValueError("trusted_execution_context_required")
        if previous is not None and not isinstance(previous, ExecutionSnapshot):
            raise ValueError("invalid_previous_snapshot")
        path = "/v1/cutovers"
        params = {"plan_id": expected.plan_id, "epoch": expected.epoch}
        if expected.execution_id is not None:
            path += f"/{expected.execution_id}"
            params = None
        return self._request("GET", path, expected, params=params, previous=previous,
                             list_response=expected.execution_id is None)

    def _request(self, method, path, expected, *, params=None, body=None, headers=None,
                 previous=None, list_response=False, accepted_statuses=(200,)):
        started = time.monotonic()
        try:
            with self._client.stream(method, path, params=params, json=body, headers=headers) as response:
                if response.status_code not in accepted_statuses:
                    return _unknown("http_status")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return _unknown("invalid_response")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return _unknown("invalid_response")
                body = bytearray()
                # Check each received chunk; batching to 8 KiB can hide a slow drip.
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > self._timeout:
                        return _unknown("read_timeout")
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        return _unknown("response_too_large")
            value = json.loads(body, object_pairs_hook=_object)
            if list_response:
                if not isinstance(value, list):
                    return _unknown("invalid_response")
                if len(value) != 1:
                    return _unknown("not_found" if not value else "ambiguous_execution")
                value = value[0]
            snapshot = _snapshot(value, expected)
            if previous is not None and not _extends(snapshot, previous):
                return _unknown("history_mismatch")
            return ExecutionRead("OBSERVED", snapshot=snapshot)
        except httpx.HTTPError:
            return _unknown("transport_failure")
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
            # Never return raw provider bodies, exception text, URLs, or credentials.
            return _unknown("invalid_response")


class ExecutorClient(ExecutorReader):
    """One POST only after the caller durably claims a verified CUTOVER command.

    Every ambiguous response is UNKNOWN. This class never retries a POST; the
    host resumes through the inherited read path and its saved plan/epoch.
    """

    def submit(self, expected: ExpectedExecution, command: dict, *, idempotency_key: str):
        if not isinstance(expected, ExpectedExecution):
            raise ValueError("trusted_execution_context_required")
        validate_identifier(idempotency_key)
        if (not isinstance(command, dict) or command.get("plan_id") != expected.plan_id
                or type(command.get("epoch")) is not int or command["epoch"] != expected.epoch):
            raise ValueError("command_context_mismatch")
        return self._request("POST", "/v1/cutovers", expected, body=command,
                             headers={"Idempotency-Key": idempotency_key},
                             accepted_statuses=(200, 202))
