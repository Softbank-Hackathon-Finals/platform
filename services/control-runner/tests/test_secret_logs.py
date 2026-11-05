import hashlib
import json
import secrets
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import botocore.session
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from bravo_controls.audit_probes import AuditEvent
from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.secret_logs import (
    SPEC,
    CloudWatchSecretLogSearch,
    LogCanaryReceipt,
    LogWindow,
    SecretLogProbe,
)

START = datetime(2026, 11, 6, tzinfo=UTC)
END = START + timedelta(seconds=5)
SETTLED = END + timedelta(seconds=5)
NOW = SETTLED + timedelta(seconds=1)
CTX = RunContext("run-1", "workload-1", Environment("source", "source", "aws:eks/example"))
SOURCE = "arn:aws:logs:ap-northeast-2:123456789012:log-group:example-app"
STREAMS = ("example-app-stdout", "example-app-stderr")
CONFIG = "sha256:" + "c" * 64
SECRET = "bravo_secret_" + "a" * 64
MARKER = "bravo_log_" + "b" * 32


def receipt(**kw):
    return replace(LogCanaryReceipt(CTX, SOURCE, STREAMS, CONFIG, SECRET, MARKER,
                                   START, END, SETTLED,
                                   frozenset({"configured", "exercised", "restored", "logs_drained"})), **kw)


def event(id="1", stream=STREAMS[0], text=MARKER):
    return AuditEvent(id, stream, START + timedelta(seconds=1), END, text)


def batch(**kw):
    return replace(LogWindow(CTX, SOURCE, STREAMS, START, END, NOW,
                             (event(), event("2", STREAMS[1])), True, True), **kw)


def probe(r=None, b=None, evidence=None, search=None, **kw):
    return SecretLogProbe(receipt_provider=lambda *_: r or receipt(),
                          search=search or (lambda _: b or batch()),
                          source_binding=SOURCE, streams=STREAMS, configuration_sha256=CONFIG,
                          evidence_sink=(evidence if evidence is not None else []).append,
                          clock=lambda: NOW, **kw)


def test_clean_full_window_passes_and_evidence_has_no_raw_log_or_secret():
    evidence = []
    result = probe(evidence=evidence)(CTX, SPEC)
    assert (result.observed, result.positive_control, result.unknown_reason) == ("absent", "pass", None)
    assert result.output_sha256 == "sha256:" + hashlib.sha256(evidence[0]).hexdigest()
    for private in (SECRET, MARKER, SOURCE, *STREAMS):
        assert private not in evidence[0].decode()
    assert json.loads(evidence[0])["searched_events"] == 2
    assert SECRET not in repr(receipt())
    assert MARKER not in repr(batch())


def test_secret_without_positive_marker_on_same_event_is_still_detected():
    evidence = []
    result = probe(b=batch(events=(*batch().events, event("3", text=SECRET))), evidence=evidence)(CTX, SPEC)
    assert result.observed == "present"
    assert result.positive_control == "pass"
    assert json.loads(evidence[0])["leaking_events"] == 1
    assert SECRET not in evidence[0].decode()


@pytest.mark.parametrize("events", [(), (event(),), (event("3", text=SECRET),)])
def test_every_stream_needs_positive_control(events):
    result = probe(b=batch(events=events))(CTX, SPEC)
    assert result.observed is None
    assert result.positive_control == "fail"
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"


@pytest.mark.parametrize("changes", [
    {"context": replace(CTX, run_id="other")}, {"source_binding": "other"},
    {"streams": (STREAMS[0],)}, {"configuration_sha256": "sha256:" + "d" * 64},
    {"settled_at": NOW + timedelta(seconds=1)},
    {"started_at": START - timedelta(minutes=20), "ended_at": END - timedelta(minutes=20),
     "settled_at": SETTLED - timedelta(minutes=20)},
])
def test_receipt_cannot_be_replayed_or_rebound(changes):
    evidence = []
    with pytest.raises(ValueError):
        probe(r=receipt(**changes), evidence=evidence)(CTX, SPEC)
    assert evidence == []


@pytest.mark.parametrize("changes", [
    {"acknowledged": frozenset({"configured", "exercised", "restored"})},
    {"acknowledged": {"configured", "exercised", "restored", "logs_drained"}},
    {"secret": "actual-short-password"}, {"marker": SECRET},
    {"started_at": END}, {"settled_at": END - timedelta(seconds=1)},
    {"settled_at": START + timedelta(minutes=11)}, {"streams": (STREAMS[0], STREAMS[0])},
])
def test_effect_receipt_requires_closed_qualified_window(changes):
    with pytest.raises(ValueError):
        receipt(**changes)


@pytest.mark.parametrize("changes", [
    {"context": replace(CTX, app="other")}, {"source_binding": "other"},
    {"streams": tuple(reversed(STREAMS))}, {"started_at": START - timedelta(seconds=1)},
    {"ended_at": END - timedelta(seconds=1)}, {"queried_at": SETTLED},
    {"queried_at": NOW + timedelta(seconds=1)}, {"complete": False}, {"complete": 1},
    {"raw_view": False}, {"raw_view": 1}, {"events": list(batch().events)},
    {"events": (event(), event())}, {"events": (event(text="x" * (10 * 1024 * 1024 + 1)),)},
    {"events": (replace(event(), timestamp=START - timedelta(seconds=1)),)},
    {"events": (replace(event(), ingested_at=NOW + timedelta(seconds=1)),)},
    {"events": (replace(event(), stream="unregistered"),)},
])
def test_incomplete_redacted_or_mismatched_search_cannot_pass(changes):
    with pytest.raises(ValueError):
        probe(b=batch(**changes))(CTX, SPEC)


def test_runner_hides_collector_exception_and_preserves_missing_tests():
    controls = Path(__file__).resolve().parents[3] / "docs" / "controls"
    catalog = Catalog(json.loads((controls / "profiles.json").read_text()),
                      json.loads((controls / "control-result.schema.json").read_text()))

    def denied(_):
        raise PermissionError(SECRET)

    run = Runner(catalog, {SPEC.test_id: probe(search=denied)}, clock=lambda: NOW).run(
        profile_id="full", profile_version="1", app=CTX.app, environment=CTX.environment,
        registered_context=CTX)
    assert len(run["results"]) == 20
    row = next(r for r in run["results"] if r["test_id"] == SPEC.test_id)
    assert row["verdict"] == "UNKNOWN"
    assert row["unknown_reason"] == "OBSERVATION_DENIED"
    assert SECRET not in json.dumps(run)


def test_evidence_storage_failure_is_not_success():
    def fail(_):
        raise OSError("storage unavailable")

    p = probe()
    p.sink = fail
    with pytest.raises(OSError):
        p(CTX, SPEC)


def client():
    return botocore.session.get_session().create_client(
        "logs", region_name="ap-northeast-2", aws_access_key_id="testing",
        aws_secret_access_key="testing", config=Config(connect_timeout=1, read_timeout=1,
        retries={"mode": "standard", "total_max_attempts": 1}))


def query():
    return {"logGroupIdentifier": SOURCE, "logStreamNames": list(STREAMS), "unmask": True,
            "limit": 1000, "startTime": int(START.timestamp() * 1000),
            "endTime": int(END.timestamp() * 1000)}


def page(rows=(), **kw):
    return {"ResponseMetadata": {"HTTPStatusCode": 200}, "events": list(rows), **kw}


def wire(e):
    return {"eventId": e.id, "message": e.message, "logStreamName": e.stream,
            "timestamp": int(e.timestamp.timestamp() * 1000),
            "ingestionTime": int(e.ingested_at.timestamp() * 1000)}


def test_cloudwatch_empty_page_is_not_end_and_secret_is_not_a_query_term():
    c = client()
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS, clock=lambda: NOW)
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(nextToken="more"), query())
        stub.add_response("filter_log_events", page([wire(e) for e in batch().events]),
                          {**query(), "nextToken": "more"})
        assert probe(search=search)(CTX, SPEC).observed == "absent"
        stub.assert_no_pending_responses()


def test_cloudwatch_missing_unmask_permission_is_unknown_not_redacted_fallback():
    c = client()
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS, clock=lambda: NOW)
    with Stubber(c) as stub:
        stub.add_client_error("filter_log_events", "AccessDeniedException", SECRET,
                              http_status_code=400, expected_params=query())
        with pytest.raises(PermissionError, match="^log query denied$"):
            search(receipt())


def test_cloudwatch_repeated_token_rejected():
    c = client()
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS, clock=lambda: NOW)
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(nextToken="same"), query())
        stub.add_response("filter_log_events", page(nextToken="same"), {**query(), "nextToken": "same"})
        with pytest.raises(ValueError, match="pagination"):
            search(receipt())


def test_cloudwatch_unfinished_pagination_cannot_pass_at_budget():
    c = client()
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS, clock=lambda: NOW)
    with Stubber(c) as stub:
        for i in range(10):
            stub.add_response("filter_log_events", page(nextToken=f"page-{i}"),
                              query() if i == 0 else {**query(), "nextToken": f"page-{i - 1}"})
        with pytest.raises(ValueError, match="page limit"):
            search(receipt())
        stub.assert_no_pending_responses()


def test_cloudwatch_timeout_after_response_is_not_complete():
    c = client()
    times = iter((0, 0, 31))
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS,
                                      clock=lambda: NOW, monotonic=lambda: next(times))
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(), query())
        with pytest.raises(TimeoutError):
            search(receipt())


def test_cloudwatch_does_not_round_away_part_of_window():
    search = CloudWatchSecretLogSearch(client(), log_group_arn=SOURCE, streams=STREAMS,
                                      clock=lambda: NOW)
    with pytest.raises(ValueError):
        search(receipt(ended_at=END + timedelta(microseconds=1)))


def test_cloudwatch_wrong_stream_cannot_supply_positive_control():
    c = client()
    search = CloudWatchSecretLogSearch(c, log_group_arn=SOURCE, streams=STREAMS, clock=lambda: NOW)
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page([wire(event(stream="foreign"))]), query())
        with pytest.raises(ValueError, match="envelope"):
            search(receipt())


@pytest.mark.parametrize("leak", [False, True])
def test_actual_local_process_log_capture(leak):
    """Actual child stdout/stderr, not the deployed demo-chat or CloudWatch."""
    secret, marker = "bravo_secret_" + secrets.token_hex(32), "bravo_log_" + secrets.token_hex(16)
    script = """
import json, sys
settings = json.load(sys.stdin)
print(settings['marker'])
print(settings['marker'], file=sys.stderr)
if settings['leak']:
    print(settings['secret'])
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], input=json.dumps({
        "marker": marker, "secret": secret, "leak": leak}), text=True, capture_output=True,
        timeout=5, check=True, env={})
    logs = tuple(event(str(i), stream, text) for i, (stream, text) in enumerate(
        ((STREAMS[0], result.stdout), (STREAMS[1], result.stderr))))
    evidence = []
    observed = probe(r=receipt(secret=secret, marker=marker), b=batch(events=logs), evidence=evidence)(CTX, SPEC)
    assert observed.observed == ("present" if leak else "absent")
    assert secret not in evidence[0].decode()
