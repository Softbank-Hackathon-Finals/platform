"""T12 response-loss reads and failure boundaries; no live control operations."""

import json
import threading
import time
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from bravo_platform_events.executor import (
    MAX_RESPONSE_BYTES,
    ExecutionRead,
    ExecutorReader,
    ExpectedExecution,
)

EXECUTION = "aaaaaaaa-aaaa-4aaa-8aaa-abcdefabcdef"
EXPECTED = ExpectedExecution("plan-demo", 7, EXECUTION)


def response_value():
    # Published T12 Execution.view() shape (#116 cccddc0), including unsafe fields.
    return {
        "execution_id": EXECUTION, "plan_id": "plan-demo", "epoch": 7,
        "status": "RUNNING", "committed": False,
        "created_at": "2026-11-03T10:00:00+00:00", "updated_at": "2026-11-03T10:00:01+00:00",
        "steps": [{"step": "precheck", "status": "succeeded", "elapsed_ms": 12,
                   "detail": "PRIVATE_DETAIL_SENTINEL"}],
        "result": {"error": "PRIVATE_RESULT_SENTINEL"},
    }


def observe(value, *, expected=EXPECTED, previous=None, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=value)

    with ExecutorReader("https://executor.example.invalid", transport=httpx.MockTransport(handle)) as reader:
        result = reader.read(expected, previous=previous)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    return result


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "DONE", "ABORT_PRE_WRITE", "SAFE_HOLD"])
def test_remote_states_preserved_without_authorizing_or_completing_plan(status):
    value = response_value()
    value.update(status=status, committed=status == "DONE")
    result = observe(value)
    assert result.outcome == "OBSERVED"
    assert result.snapshot.remote_status == status
    assert result.snapshot.plan_id == EXPECTED.plan_id
    assert "PRIVATE" not in json.dumps(asdict(result))
    assert not hasattr(result, "can_resume")


@pytest.mark.parametrize("values,reason", [([], "not_found"), ([{}, {}], "ambiguous_execution")])
def test_response_loss_does_not_start_another_execution(values, reason):
    result = observe(values, expected=replace(EXPECTED, execution_id=None))
    assert result == ExecutionRead("UNKNOWN", reason)


def test_response_loss_requires_single_matching_execution():
    found = observe([response_value()], expected=replace(EXPECTED, execution_id=None))
    assert found.snapshot.execution_id == EXECUTION
    value = response_value()
    value["epoch"] = 8
    assert observe([value], expected=replace(EXPECTED, execution_id=None)).outcome == "UNKNOWN"


@pytest.mark.parametrize("field,value", [
    ("plan_id", "another-plan"), ("epoch", 8), ("epoch", True), ("epoch", "7"),
    ("execution_id", "aaaaaaaa-aaaa-4aaa-8aaa-abcdefabcdea"),
    ("execution_id", "../secrets"), ("status", "SUCCEEDED"), ("status", []),
    ("committed", 1), ("created_at", "2026-11-03T10:00:00"),
    ("updated_at", "2026-11-03T09:59:59Z"), ("steps", {}),
    ("steps", [{"step": "precheck", "status": "PASS"}]),
    ("steps", [{"step": "precheck", "status": "failed", "elapsed_ms": True}]),
    ("steps", [{"step": "precheck", "status": "failed", "elapsed_ms": -1}]),
    ("steps", [{"step": "precheck", "status": "failed", "elapsed_ms": None}]),
    ("steps", [{"step": "precheck", "status": "failed", "elapsed_ms": 1.2}]),
    ("steps", [{"step": "PRIVATE@example.invalid", "status": "failed"}]),
    ("steps", [{"step": "pause", "status": "started"}] * 257),
])
def test_mismatched_or_malformed_response_is_unknown(field, value):
    body = response_value()
    body[field] = value
    assert observe(body) == ExecutionRead("UNKNOWN", "invalid_response")


@pytest.mark.parametrize("missing", ["execution_id", "plan_id", "epoch", "status", "steps", "committed", "created_at", "updated_at"])
def test_required_fields_cannot_default_to_success(missing):
    value = response_value()
    del value[missing]
    assert observe(value).outcome == "UNKNOWN"


@pytest.mark.parametrize("status,committed", [("DONE", False), ("ABORT_PRE_WRITE", True)])
def test_inconsistent_commit_milestone_is_unknown(status, committed):
    value = response_value()
    value.update(status=status, committed=committed)
    assert observe(value).outcome == "UNKNOWN"


def test_append_only_steps_and_repeated_read_are_stable():
    value = response_value()
    original = observe(value).snapshot
    assert observe(value, previous=original).snapshot == original
    value["steps"].append({"step": "pause", "status": "started"})
    value["updated_at"] = "2026-11-03T10:00:02Z"
    next_snapshot = observe(value, previous=original).snapshot
    assert len(next_snapshot.steps) == 2
    assert next_snapshot.steps[0] == original.steps[0]


@pytest.mark.parametrize("change", ["shorten", "rewrite", "time", "created", "pending", "identity", "commit"])
def test_stale_or_rewritten_history_is_unknown(change):
    value = response_value()
    value["committed"] = True
    previous = observe(value).snapshot
    if change == "shorten":
        value["steps"] = []
    elif change == "rewrite":
        value["steps"][0]["status"] = "failed"
    elif change == "time":
        value["updated_at"] = value["created_at"]
    elif change == "created":
        value["created_at"] = "2026-11-03T09:00:00Z"
    elif change == "pending":
        value["status"] = "PENDING"
    elif change == "identity":
        previous = replace(previous, epoch=6)
    else:
        value["committed"] = False
    assert observe(value, previous=previous) == ExecutionRead("UNKNOWN", "history_mismatch")


@pytest.mark.parametrize("held", ["SAFE_HOLD", "DONE", "ABORT_PRE_WRITE"])
def test_terminal_or_held_observation_does_not_reopen(held):
    value = response_value()
    value.update(status=held, committed=held == "DONE")
    previous = observe(value).snapshot
    value["status"] = "RUNNING"
    assert observe(value, previous=previous).outcome == "UNKNOWN"


@pytest.mark.parametrize("status", [202, 204, 301, 302, 307, 401, 403, 404, 409, 422, 429, 500, 503])
def test_non_200_never_becomes_an_execution_result(status):
    assert observe({"detail": "PRIVATE_SENTINEL"}, status=status) == ExecutionRead("UNKNOWN", "http_status")


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transport_errors_are_redacted_and_never_retried(failure):
    seen = []

    def broken(request):
        seen.append(request)
        raise failure("PRIVATE_CONNECTION_SENTINEL", request=request)

    with ExecutorReader("https://executor.example.invalid", transport=httpx.MockTransport(broken)) as reader:
        result = reader.read(EXPECTED)
    assert result == ExecutionRead("UNKNOWN", "transport_failure")
    assert len(seen) == 1


@pytest.mark.parametrize("body,content_type,reason", [
    (b"not json", "application/json", "invalid_response"),
    (b"[]", "text/html", "invalid_response"),
    (b'{"status":"DONE","status":"RUNNING"}', "application/json", "invalid_response"),
    (b" " * (MAX_RESPONSE_BYTES + 1), "application/json", "response_too_large"),
])
def test_wire_size_type_and_duplicate_keys(body, content_type, reason):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body, headers={"Content-Type": content_type}))
    with ExecutorReader("https://executor.example.invalid", transport=transport) as reader:
        assert reader.read(EXPECTED) == ExecutionRead("UNKNOWN", reason)


@pytest.mark.parametrize("origin", [
    "http://executor.example.invalid", "https://user:password@example.invalid",
    "https://example.invalid?token=private", "https://example.invalid#fragment",
    "https://example.invalid/v1", "file:///tmp/private", "https://example.invalid:0",
])
def test_origin_does_not_accept_credentials_or_unverified_remote_http(origin):
    with pytest.raises(ValueError, match="invalid_executor_origin"):
        ExecutorReader(origin)


def test_compressed_response_is_rejected_before_decoding():
    import gzip

    body = gzip.compress(b" " * (MAX_RESPONSE_BYTES * 10))
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, stream=httpx.ByteStream(body),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ))
    with ExecutorReader("https://executor.example.invalid", transport=transport) as reader:
        assert reader.read(EXPECTED) == ExecutionRead("UNKNOWN", "invalid_response")


@pytest.mark.parametrize("token", ["", "space token", "line\nbreak", "nul\x00", "한글", "x" * 8193])
def test_invalid_tokens_are_rejected_without_echo(token):
    with pytest.raises(ValueError, match="^invalid_executor_token$"):
        ExecutorReader("https://executor.example.invalid", token=token)


@pytest.mark.parametrize("epoch", [True, 0, -1, "7", 2**63])
def test_server_context_rejects_invalid_epoch(epoch):
    with pytest.raises(ValueError, match="invalid_epoch"):
        ExpectedExecution("plan-demo", epoch)


def test_real_http_get_query_no_redirect_and_new_reader_recovery(monkeypatch):
    """Real TCP path. A new client reuses trusted context after process-local loss."""
    requests = []
    body = response_value()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            if len(requests) == 3:
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/should-not-follow")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([body] if "?" in self.path else body).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Host proxy settings cannot silently reroute executor credentials.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        origin = f"http://127.0.0.1:{server.server_port}"
        with ExecutorReader(origin) as first:
            found = first.read(replace(EXPECTED, execution_id=None))
        with ExecutorReader(origin) as second:
            recovered = second.read(EXPECTED, previous=found.snapshot)
            redirect = second.read(EXPECTED)
        assert found.outcome == recovered.outcome == "OBSERVED"
        assert recovered.snapshot == found.snapshot
        assert redirect == ExecutionRead("UNKNOWN", "http_status")
        assert requests == [
            ("GET", "/v1/cutovers?plan_id=plan-demo&epoch=7"),
            ("GET", f"/v1/cutovers/{EXECUTION}"),
            ("GET", f"/v1/cutovers/{EXECUTION}"),
        ]
        assert body == response_value()  # Observer does not mutate producer data.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_slow_drip_is_checked_before_accumulating_a_large_chunk():
    """Each byte arrives inside the socket timeout; the stream still must stop."""
    sent = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            for _ in range(60):
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    sent.append(1)
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.03)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExecutorReader(f"http://127.0.0.1:{server.server_port}", timeout_seconds=0.2) as reader:
            started = time.monotonic()
            result = reader.read(EXPECTED)
            elapsed = time.monotonic() - started
        assert result == ExecutionRead("UNKNOWN", "read_timeout")
        # Allow scheduling overhead while rejecting the previous 1.8-second buffering.
        assert elapsed < 0.8
        assert len(sent) < 60
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
