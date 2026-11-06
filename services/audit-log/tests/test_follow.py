"""``bravo-audit follow`` against the real T10 event API (create_app + PlanStore, unchanged).

Cases the real T10 cannot produce today (``data.tool_events``, ``400 invalid_cursor`` from a
stored cursor, malformed streams) use a stdlib SSE server speaking the same wire format."""

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from t10_events_server import T10Server, t10

from bravo_audit import follow as follow_mod
from bravo_audit.cli import main
from bravo_audit.follow import FollowStop, Upstream, parse_sse
from bravo_audit.sink import FileSink

_api, _store, plans = t10()
PLAN = "plan-follow"
RUN = "run-follow-1"
TOKEN = "t10-read-credential-for-tests"
TIME = "2026-11-06T10:00:00Z"


# --- real T10 ------------------------------------------------------------------------------

@pytest.fixture
def t10_store(tmp_path):
    store = plans.PlanStore(tmp_path / "t10.sqlite")
    spec = plans.PlanSpec(PLAN, "tenant-demo", "workload-demo", "env-source", "env-target", "a" * 64, "b" * 64)
    store.register_plan(spec, run_id=RUN, occurred_at=TIME)  # T10 seq 1: state.changed -> COLLECT
    return store


def gate(store, n, run=RUN):
    return store.append(plan_id=PLAN, run_id=run, event_type="gate.completed", occurred_at=TIME,
                        producer_key=f"gate-{n}",
                        data={"gate_id": f"gate-{n}", "evidence_ref": f"evidence-{n}", "decision": "PASS",
                              "context_digest": "4" * 64})


def execution(store, n, run=RUN):
    return store.append(plan_id=PLAN, run_id=run, event_type="execution.updated", occurred_at=TIME,
                        producer_key=f"execution-{n}",
                        data={"execution_id": "execution-1", "step": f"step-{n}", "status": "started"})


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "t10.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def server(t10_store):
    with T10Server(t10_store, TOKEN) as s:
        yield s


def run_follow(base_url, sink, token_file, *extra, plan=PLAN):
    args = ["follow", "--base-url", base_url, "--plan", plan, "--sink", str(sink), "--read-timeout", "10"]
    if token_file is not None:
        args += ["--auth-token-file", str(token_file)]
    return main(args + list(extra))


def stored(sink, plan=PLAN):
    root = Path(sink) / "audit" / plan
    return {run.name: [json.loads(p.read_text()) for p in sorted(run.glob("*.json"))]
            for run in sorted(root.iterdir())} if root.exists() else {}


def summary(capsys):
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err  # never logged
    return json.loads(captured.out.strip().splitlines()[-1]), captured


def test_catch_up_resume_and_redelivery_against_the_real_t10_api(tmp_path, server, t10_store, token_file, capsys):
    sink = tmp_path / "store"
    gate(t10_store, 1)
    execution(t10_store, 1)
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    out, _ = summary(capsys)
    assert (out["records"], out["last_t10_sequence"], out["resumed_after_sequence"]) == (3, 3, 0)
    records = stored(sink)[RUN]
    assert [r["kind"] for r in records] == ["state.changed", "gate.completed", "execution.updated"]
    assert [r["origin_id"] for r in records] == [f"t10:{PLAN}:{n}" for n in (1, 2, 3)]
    # Nothing new: the stored cursor is sent and nothing is written.
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    out, _ = summary(capsys)
    assert out["records"] == 0 and out["resumed_after_sequence"] == 3
    # More events after a restart: only those are appended, continuing the same chain.
    gate(t10_store, 2)
    execution(t10_store, 2)
    assert run_follow(server.base_url, sink, token_file, "--once", "--receipts", str(tmp_path / "r.jsonl")) == 0
    out, _ = summary(capsys)
    assert out["records"] == 2 and out["last_t10_sequence"] == 5
    assert server.cursors == [None, f"{PLAN}:3", f"{PLAN}:3"]
    assert set(server.presented) == {f"Bearer {TOKEN}"}  # the configured header reached T10
    receipts = [json.loads(line) for line in (tmp_path / "r.jsonl").read_text().splitlines()]
    assert [r["t10_sequence"] for r in receipts] == [4, 5] and (tmp_path / "r.jsonl").stat().st_mode & 0o777 == 0o600
    assert main(["verify", str(sink / "audit" / PLAN / RUN)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["records"] == 5 and verified["chain_digest"] == out["runs"][RUN]


def test_events_of_several_runs_go_to_their_own_chains(tmp_path, server, t10_store, token_file, capsys):
    gate(t10_store, 1, run="run-follow-2")
    execution(t10_store, 1)
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once") == 0
    runs = stored(tmp_path / "s")
    assert {k: [r["sequence"] for r in v] for k, v in runs.items()} == {RUN: [1, 2], "run-follow-2": [1]}
    gate(t10_store, 2, run="run-follow-2")
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once") == 0
    out, _ = summary(capsys)
    assert out["records"] == 1 and server.cursors[-1] == f"{PLAN}:3"
    assert len(stored(tmp_path / "s")["run-follow-2"]) == 2


def test_kill_mid_stream_then_restart_has_no_duplicates_and_no_gaps(tmp_path, server, t10_store, token_file):
    """A continuous follower (follow=true) is SIGKILLed while T10 keeps producing; the restart
    resumes from the files actually written."""
    sink = tmp_path / "store"
    for n in range(1, 4):
        gate(t10_store, n)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    proc = subprocess.Popen([sys.executable, "-m", "bravo_audit.cli", "follow", "--base-url", server.base_url,
                             "--plan", PLAN, "--sink", str(sink), "--auth-token-file", str(token_file)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        n = 3
        while time.monotonic() < deadline and len(stored(sink).get(RUN, [])) < 8:
            n += 1
            gate(t10_store, n)  # produced while the follower streams
            time.sleep(0.05)
        assert len(stored(sink).get(RUN, [])) >= 4
    finally:
        proc.send_signal(signal.SIGKILL)
        out, err = proc.communicate(timeout=10)
    assert TOKEN.encode() not in out + err
    for m in range(n + 1, n + 4):
        gate(t10_store, m)
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    records = stored(sink)[RUN]
    total = n + 4  # registration + n gates + 3 gates after the kill
    assert [r["origin_id"] for r in records] == [f"t10:{PLAN}:{k}" for k in range(1, total + 1)]
    assert main(["verify", str(sink / "audit" / PLAN / RUN)]) == 0


def test_a_failed_write_stops_and_the_restart_completes_from_storage(tmp_path, server, t10_store, token_file,
                                                                     monkeypatch, capsys):
    for n in range(1, 4):
        gate(t10_store, n)
    real_put, calls = FileSink.put, []

    def flaky(self, record):
        calls.append(record.sequence)
        if len(calls) == 3:
            raise OSError("disk went away")
        return real_put(self, record)

    monkeypatch.setattr(FileSink, "put", flaky)
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once") == follow_mod.EXIT_WRITE
    out, captured = summary(capsys)
    assert out["reason"] == "write_not_confirmed:OSError" and "disk went away" not in captured.err
    assert len(stored(tmp_path / "s")[RUN]) == 2
    monkeypatch.setattr(FileSink, "put", real_put)
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once") == 0
    assert [r["origin_id"] for r in stored(tmp_path / "s")[RUN]] == [f"t10:{PLAN}:{k}" for k in range(1, 5)]
    assert server.cursors[-1] == f"{PLAN}:2"


@pytest.mark.parametrize("damage", ["edit", "remove_middle", "foreign_folder"])
def test_a_tampered_store_is_refused_before_connecting_or_writing(tmp_path, server, t10_store, token_file,
                                                                   capsys, damage):
    sink = tmp_path / "store"
    gate(t10_store, 1)
    gate(t10_store, 2)
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    run_dir = sink / "audit" / PLAN / RUN
    files = sorted(run_dir.glob("*.json"))
    if damage == "edit":
        doc = json.loads(files[1].read_text())
        doc["payload"]["data"]["decision"] = "BLOCK"
        files[1].chmod(0o600)
        files[1].write_text(json.dumps(doc))
    elif damage == "remove_middle":
        files[1].unlink()
    else:
        other = sink / "audit" / PLAN / "run-other"
        other.mkdir()
        (other / "00000001.json").write_text(files[0].read_text())
    gate(t10_store, 3)
    before = sorted(p.name for p in sink.rglob("*.json"))
    requests = len(server.presented)
    capsys.readouterr()
    assert run_follow(server.base_url, sink, token_file, "--once") == follow_mod.EXIT_STORE
    out, _ = summary(capsys)
    assert out["reason"].startswith("stored_chain_untrusted")
    assert sorted(p.name for p in sink.rglob("*.json")) == before
    assert len(server.presented) == requests  # T10 was not even contacted


def test_access_errors_stop_without_writing(tmp_path, server, token_file, capsys):
    wrong = tmp_path / "wrong.token"
    wrong.write_text("not-the-credential")
    assert run_follow(server.base_url, tmp_path / "s", wrong, "--once") == follow_mod.EXIT_ACCESS
    assert summary(capsys)[0]["reason"] == "forbidden"
    assert run_follow(server.base_url, tmp_path / "s", None, "--once") == follow_mod.EXIT_ACCESS
    assert server.presented[-1] is None
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once", plan="plan-unknown") == follow_mod.EXIT_ACCESS
    assert summary(capsys)[0]["reason"] == "plan_not_found"
    assert not (tmp_path / "s").exists()


def test_resync_required_stops_instead_of_rereading_from_the_start(tmp_path, server, t10_store, token_file, capsys):
    sink = tmp_path / "store"
    gate(t10_store, 1)
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    gate(t10_store, 2)
    # T10 lost the event the stored cursor points at (retention / restore from an older backup).
    with sqlite3.connect(t10_store.path) as c:
        c.execute("DELETE FROM events WHERE plan_id=? AND sequence=2", (PLAN,))
    capsys.readouterr()
    assert run_follow(server.base_url, sink, token_file, "--once") == follow_mod.EXIT_RESYNC
    assert summary(capsys)[0]["reason"] == "resync_required"
    assert len(stored(sink)[RUN]) == 2  # nothing re-read, nothing forked
    assert server.cursors[-1] == f"{PLAN}:2"


def test_real_t10_does_not_accept_tool_events_yet(t10_store):
    """Why the tool_events cases below use the stub: T10 schema 1.0 closes ``data``."""
    with pytest.raises(Exception, match="tool_events"):
        t10_store.append(plan_id=PLAN, run_id=RUN, event_type="gate.completed", occurred_at=TIME,
                         producer_key="g", data={"gate_id": "g", "evidence_ref": "e", "decision": "PASS",
                                                 "context_digest": "4" * 64, "tool_events": []})


# --- stdlib SSE stub (same wire format as T10 encode_sse) ----------------------------------

def t10_event(seq, event_type="gate.completed", data=None, run=RUN):
    return {"schema_version": "1.0", "event_id": f"{PLAN}:{seq}", "sequence": seq, "plan_id": PLAN,
            "run_id": run, "occurred_at": TIME, "event_type": event_type,
            "data": data if data is not None else {"gate_id": f"g{seq}", "decision": "PASS"}}


def sse(*events):
    out = "retry: 1000\n\n"
    for e in events:
        out += f"id: {e['event_id']}\nevent: {e['event_type']}\ndata: {json.dumps(e)}\n\n"
    return out.encode()


class Stub:
    """Serves scripted responses; each script entry is (status, content_type, body) or a
    callable taking the request headers."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                stub.requests.append({"path": self.path, "headers": dict(self.headers)})
                entry = stub.script.pop(0) if stub.script else (503, "application/json", b"{}")
                if callable(entry):
                    entry = entry(self.headers)
                status, ctype, body = entry
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if status in (301, 302):
                    self.send_header("Location", "http://198.51.100.7/steal")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    made = []

    def make(*script):
        made.append(Stub(script))
        return made[-1]
    yield make
    for s in made:
        s.close()


SSE = "text/event-stream"
TOOLS = [
    {"run_id": RUN, "attempt": 1, "tool": "repo.read", "outcome": "ALLOWED", "reason": "POLICY_ACCEPTED"},
    {"run_id": RUN, "attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
     "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"},
    {"run_id": RUN, "attempt": 1, "tool": "net.fetch", "outcome": "DENIED", "reason": "TOOL_NOT_ALLOWED"},
]


def test_tool_events_are_split_end_to_end_and_a_crash_mid_split_is_completed(tmp_path, stub, token_file,
                                                                             monkeypatch, capsys):
    events = [t10_event(1, "state.changed", {"from": None, "to": "COLLECT"}),
              t10_event(2, data={"gate_id": "g2", "decision": "PASS", "tool_events": TOOLS,
                                 "tool_events_status": "COMPLETE"})]
    server = stub((200, SSE, sse(*events)), (200, SSE, sse(events[1])), (200, SSE, b"retry: 1000\n\n"))
    real_put, calls = FileSink.put, []

    def crash_after_head_and_first_tool(self, record):
        calls.append(record.kind)
        if len(calls) == 4:
            raise OSError("killed")
        return real_put(self, record)

    monkeypatch.setattr(FileSink, "put", crash_after_head_and_first_tool)
    sink = tmp_path / "s"
    assert run_follow(server.base_url, sink, token_file, "--once") == follow_mod.EXIT_WRITE
    assert [r["kind"] for r in stored(sink)[RUN]] == ["state.changed", "gate.completed", "POLICY_ACCEPTED"]
    monkeypatch.setattr(FileSink, "put", real_put)
    capsys.readouterr()
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    out, _ = summary(capsys)
    # The split event was requested again (cursor moved back by one) and completed exactly once.
    assert server.requests[1]["headers"]["Last-Event-ID"] == f"{PLAN}:1"
    assert (out["records"], out["absorbed_redeliveries"]) == (2, 2)
    records = stored(sink)[RUN]
    assert [r["kind"] for r in records] == ["state.changed", "gate.completed", "POLICY_ACCEPTED",
                                            "DESTRUCTIVE_ATTEMPT_BLOCKED", "TOOL_NOT_ALLOWED"]
    assert records[1]["payload"]["data"]["tool_events_count"] == 3
    assert records[3]["origin_id"] == f"t11:{RUN}:attempt1:1"
    # Complete now: the next start still re-asks for the split event and absorbs all of it.
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    assert server.requests[2]["headers"]["Last-Event-ID"] == f"{PLAN}:1"
    assert len(stored(sink)[RUN]) == 5
    # The same attempt sent directly by T11 is absorbed too.
    tools = tmp_path / "tools.jsonl"
    tools.write_text("\n".join(json.dumps(t) for t in TOOLS))
    assert main(["append", "--source", "agent.tools", "--plan", PLAN, "--input", str(tools), "--sink", str(sink),
                 "--receipts", str(tmp_path / "r.json"), "--resume-from", str(sink / "audit" / PLAN / RUN)]) == 0
    assert json.loads((tmp_path / "r.json").read_text())["absorbed_redeliveries"] == 3


@pytest.mark.parametrize("response,code,reason", [
    ((400, "application/json", b'{"detail":{"code":"invalid_cursor"}}'), follow_mod.EXIT_RESYNC, "invalid_cursor"),
    ((409, "application/json", b'{"detail":{"code":"resync_required"}}'), follow_mod.EXIT_RESYNC, "resync_required"),
    ((400, "application/json", b'{"detail":{"code":"something <script>"}}'), follow_mod.EXIT_CONFIG, "bad_request:unknown"),
    ((302, "text/plain", b""), follow_mod.EXIT_CONFIG, "redirect_refused"),
    ((200, "application/json", b"{}"), follow_mod.EXIT_REJECTED, "not_an_event_stream"),
    ((503, "text/plain", b"down"), follow_mod.EXIT_UNAVAILABLE, "upstream_status:503"),
    ((200, SSE, sse(t10_event(1), t10_event(3))), follow_mod.EXIT_REJECTED, "sequence_out_of_order"),
    ((200, SSE, sse(t10_event(1, run="run-x"), t10_event(2, data={"tool_events": [TOOLS[0]]}, run="run-x"))),
     follow_mod.EXIT_REJECTED, "event_rejected"),
    ((200, SSE, b"id: plan-follow:1\nevent: state.changed\ndata: " + json.dumps(t10_event(1)).encode() + b"\n\n"),
     follow_mod.EXIT_REJECTED, "sse_header_mismatch"),
    ((200, SSE, b"id: x\nevent: y\nfoo: bar\ndata: {}\n\n"), follow_mod.EXIT_REJECTED, "sse_unknown_field"),
    ((200, SSE, sse(t10_event(1, data={"note": "password=hunter2hunter2"}))), follow_mod.EXIT_REJECTED, "unsafe_payload"),
    ((200, SSE, sse(t10_event(1) | {"schema_version": "9.9", "data": {"gate_id": "g1", "decision": "MAYBE"}})),
     follow_mod.EXIT_REJECTED, "unsupported_schema_version"),
])
def test_upstream_errors_map_to_distinct_exit_codes(tmp_path, stub, token_file, capsys, response, code, reason):
    server = stub(response)
    assert run_follow(server.base_url, tmp_path / "s", token_file, "--once") == code
    out, captured = summary(capsys)
    assert out["reason"] == reason and "<script>" not in captured.err and "hunter2" not in captured.err + captured.out
    if reason == "sequence_out_of_order":
        assert len(stored(tmp_path / "s")[RUN]) == 1
    elif reason == "unsupported_schema_version":
        assert stored(tmp_path / "s") == {}  # nothing recorded under a version T24 does not know
    elif reason == "redirect_refused":
        assert len(server.requests) == 1


class ClientError(Exception):
    """Stands in for botocore's ClientError (AccessDenied, throttling) without importing it."""


@pytest.mark.parametrize("error", [OSError("/private/path/of/the/store is gone"),
                                   ClientError("AccessDenied for arn:aws:iam::000000000000:role/x")])
def test_an_unreadable_store_maps_to_its_own_exit_code_in_once_mode(tmp_path, stub, token_file, capsys, error):
    server = stub((200, SSE, sse(t10_event(1))))

    def load_runs():
        raise error

    upstream = Upstream(server.base_url, PLAN, token_file=token_file, timeout=5)
    code, out = follow_mod.follow(upstream, load_runs=load_runs, sink=FileSink(tmp_path / "s"), once=True)
    assert (code, out["reason"]) == (follow_mod.EXIT_STORE_UNREADABLE, f"store_unreadable:{type(error).__name__}")
    err = capsys.readouterr().err
    assert "/private/path" not in err and "000000000000" not in err and "AccessDenied" not in err
    assert server.requests == []  # T10 is not read without a resume point from storage


def test_continuous_mode_backs_off_on_an_unreadable_store_and_resumes(tmp_path, stub, token_file, capsys):
    server = stub((200, SSE, sse(t10_event(1), t10_event(2))),
                  (403, "application/json", b'{"detail":{"code":"forbidden"}}'))
    sink, delays, calls = tmp_path / "s", [], []

    def flaky_load_runs():
        calls.append(1)
        if len(calls) in (1, 2, 4):
            raise ClientError("SlowDown")
        return follow_mod.load_plan_runs(str(sink), PLAN)

    upstream = Upstream(server.base_url, PLAN, token_file=token_file, timeout=5)
    code, out = follow_mod.follow(upstream, load_runs=flaky_load_runs, sink=FileSink(sink), once=False,
                                  sleep=delays.append)
    assert (code, out["reason"], out["records"]) == (follow_mod.EXIT_ACCESS, "forbidden", 2)
    assert len(delays) == 4 and all(0 < d <= 30 for d in delays)  # 3 store errors + 1 stream end
    assert "store_unreadable:ClientError" in capsys.readouterr().err
    assert len(server.requests) == 2


def test_continuous_mode_reconnects_with_backoff_and_resumes_from_storage(tmp_path, stub, token_file):
    server = stub((503, "text/plain", b""),
                  (200, SSE, sse(t10_event(1), t10_event(2))),  # stream ends: reconnect
                  (200, SSE, b": keep-alive\n\n" + sse(t10_event(3))[len("retry: 1000\n\n"):]),
                  (403, "application/json", b'{"detail":{"code":"forbidden"}}'))
    sink, delays = tmp_path / "s", []
    upstream = Upstream(server.base_url, PLAN, token_file=token_file, timeout=5)
    code, out = follow_mod.follow(upstream, load_runs=lambda: follow_mod.load_plan_runs(str(sink), PLAN),
                                  sink=FileSink(sink), once=False, sleep=delays.append)
    assert (code, out["reason"], out["records"]) == (follow_mod.EXIT_ACCESS, "forbidden", 3)
    assert len(delays) == 3 and all(0 < d <= 30 for d in delays)
    assert [r["headers"].get("Last-Event-ID") for r in server.requests] == [None, None, f"{PLAN}:2", f"{PLAN}:3"]
    assert all(r["path"] == f"/v1/plans/{PLAN}/events?follow=true" for r in server.requests)
    assert all(r["headers"]["Authorization"] == f"Bearer {TOKEN}" for r in server.requests)


def test_a_store_with_a_t10_gap_is_refused(tmp_path, stub, token_file):
    server = stub((200, SSE, sse(t10_event(1), t10_event(2))))
    sink = tmp_path / "s"
    assert run_follow(server.base_url, sink, token_file, "--once") == 0
    # A second run folder whose first record claims T10 seq 4: the plan's T10 sequence has a gap.
    from bravo_audit.ledger import RunLedger
    ledger = RunLedger(PLAN, "run-2")
    record, _ = ledger.append("platform.events", "gate.completed", follow_mod.expand_platform_event(
        t10_event(4, run="run-2"))[0][5], f"t10:{PLAN}:4")
    FileSink(sink).put(record)
    assert run_follow(server.base_url, sink, token_file, "--once") == follow_mod.EXIT_STORE
    assert len(server.requests) == 1


# --- parser and configuration ---------------------------------------------------------------

def lines(data: bytes):
    return io.BytesIO(data).readline


def test_sse_parser_is_strict_and_bounded():
    got = list(parse_sse(lines(b": hello\r\nretry: 5\r\n\r\nid: a\r\nevent: e\r\ndata: one\r\ndata:two\r\n\r\n")))
    assert [(e.id, e.event, e.data) for e in got] == [("a", "e", "one\ntwo")]
    # An unterminated event at EOF is never dispatched.
    assert list(parse_sse(lines(b"id: a\nevent: e\ndata: x\n"))) == []
    assert list(parse_sse(lines(b"id: a\nevent: e\ndata: x"))) == []
    for bad, reason in [(b"data: x\n\n", "sse_event_without_id"), (b"id: a\n\n", "sse_event_without_data"),
                        (b"id: a\nid: b\n", "sse_duplicate_field"), (b"data: a\rb\n", "sse_malformed_line"),
                        (b"data: \xff\n", "sse_not_utf8"), (b"x" * 70000 + b"\n", "sse_line_too_long"),
                        (b"id: a\nevent: e\n" + b"data: xxxxxxxxxx\n" * 7000, "sse_event_too_large")]:
        with pytest.raises(FollowStop) as exc:
            list(parse_sse(lines(bad)))
        assert exc.value.reason == reason


@pytest.mark.parametrize("url,reason", [("http://198.51.100.7:8080", "https_required"),
                                        ("https://t10.example.invalid/?x=1", "invalid_base_url"),
                                        ("https://user:pw@t10.example.invalid", "invalid_base_url"),
                                        ("ftp://t10.example.invalid", "invalid_base_url")])
def test_the_credential_is_only_sent_over_tls_except_to_loopback(tmp_path, token_file, url, reason, capsys):
    assert run_follow(url, tmp_path / "s", token_file, "--once") == follow_mod.EXIT_CONFIG
    assert reason in capsys.readouterr().err
    assert Upstream("http://localhost:1", PLAN).base_url and Upstream("http://[::1]:1", PLAN).base_url


def test_an_unusable_credential_file_is_a_configuration_error(tmp_path, stub, capsys):
    server = stub((200, SSE, sse(t10_event(1))))
    assert run_follow(server.base_url, tmp_path / "s", tmp_path / "missing", "--once") == follow_mod.EXIT_CONFIG
    multi = tmp_path / "multi"
    multi.write_text("line-one\nline-two\n")
    assert run_follow(server.base_url, tmp_path / "s", multi, "--once") == follow_mod.EXIT_CONFIG
    assert "line-one" not in capsys.readouterr().err
    assert server.requests == []
