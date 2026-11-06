"""``bravo-audit follow``: subscribe to T10 plan events (SSE) and append them to the T24 ledger.

T10 is not changed. This is an ordinary subscriber of ``GET /v1/plans/{plan_id}/events``.

* **Resume from storage.** Every connection (first start, restart after a crash, reconnect)
  re-reads what the sink actually holds for the plan, verifies every run's chain and sends the
  T10 ``event_id`` *before* the last recorded one as ``Last-Event-ID``. There is no local cursor
  file that could disagree with the bucket. A tampered or inconsistent store stops the follower
  before it connects or writes anything.
* **Same history or stop.** The last recorded event is always redelivered and must be absorbed
  (same run, same content). A T10 reset or restore whose new history is at least as long as
  ours would otherwise continue after our cursor without a 409; it stops with
  ``origin_conflict`` instead.
* **Exactly once in the ledger.** Records are appended through ``RunLedger`` (``origin_id``
  absorption) and written one object per record with no overwrite. An event split into
  forwarded tool records (``data.tool_events``) and cut between those writes is completed by
  the same redelivery and not left half done.
* **``--once`` confirms its end.** T10 has no end-of-stream marker and ends a stream it cannot
  continue (retention hole, store error, revoked access) like a finished one. ``--once``
  reports success only for a connection that brought nothing new.
* **No silent re-read.** ``409 resync_required`` and ``400 invalid_cursor`` mean T10 cannot
  continue from what is stored. Starting over from the beginning could fork the chain, so the
  follower stops with its own exit code and leaves the decision to an operator.
* **Nothing sensitive in output.** Logs are code-owned labels and counters. Event bodies,
  upstream error bodies and the credential never reach stdout/stderr.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import random
import re
import signal
import ssl
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import PLATFORM_SCHEMA_VERSIONS, expand_platform_event
from .ledger import OriginConflict, RunLedger, utc_now, verify_chain
from .record import UnsafePayload
from .storage import IDENTIFIER, StoreTampered, load_plan_runs

EXIT_OK = 0
EXIT_CONFIG = 2  # bad arguments, unreadable credential file, TLS verification failure, redirect
EXIT_STORE = 3  # stored chain tampered or inconsistent; nothing was written
EXIT_ACCESS = 4  # 401/403/404 from T10: credential or plan wrong
EXIT_RESYNC = 5  # 409 resync_required / 400 invalid_cursor: T10 cannot continue from storage
EXIT_REJECTED = 6  # malformed stream, out-of-order event, or content the ledger refuses
EXIT_UNAVAILABLE = 7  # --once only: T10 unreachable or the stream broke; rerun resumes
EXIT_WRITE = 8  # the sink refused or did not confirm a write; rerun resumes from storage
EXIT_STORE_UNREADABLE = 9  # --once only: storage could not be listed/read (permission, network)

MAX_LINE_BYTES = 64 * 1024  # T10 caps one event at 32 KiB of canonical input
MAX_EVENT_BYTES = 64 * 1024
MAX_ERROR_BODY = 4096
MAX_TOKEN_BYTES = 16 * 1024
ONCE_MAX_CONNECTIONS = 100  # --once keeps confirming while events still arrive, but not forever
KNOWN_CODES = frozenset({"forbidden", "plan_not_found", "invalid_cursor", "resync_required", "invalid_plan_id"})
HEADER_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
CREDENTIAL = re.compile(r"[\x21-\x7e]+\Z")


class FollowStop(Exception):
    """Stop following with an exit code and a code-owned reason label."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


class Transient(Exception):
    """The connection failed or ended; storage is intact and a new connection may resume."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StoreUnreadable(Exception):
    """Storage could not be listed or read; nothing was judged, written or requested."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SseEvent:
    id: str
    event: str
    data: str


def parse_sse(readline: Callable[[int], bytes], *, max_line: int = MAX_LINE_BYTES,
              max_event: int = MAX_EVENT_BYTES) -> Iterator[SseEvent]:
    """Strict ``text/event-stream`` parser for the T10 wire format.

    Lines end with LF or CRLF. Comments (``:``) and ``retry`` are skipped; ``data`` lines are
    joined with LF. An event is dispatched on a blank line and must carry exactly one ``id`` and
    one ``event``. Unknown fields, a bare CR, NUL, invalid UTF-8, an over-long line or event,
    and an ``id``/``event`` without data are protocol errors. An unterminated event at EOF is
    discarded, as the SSE specification requires."""
    data: list[str] = []
    size, event_id, event_type = 0, None, None
    while True:
        raw = readline(max_line + 1)
        if not raw:
            return
        if not raw.endswith(b"\n"):
            if len(raw) > max_line:
                raise FollowStop(EXIT_REJECTED, "sse_line_too_long")
            return  # connection closed inside a line: the partial event is never dispatched
        line = raw[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line or b"\x00" in line:
            raise FollowStop(EXIT_REJECTED, "sse_malformed_line")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FollowStop(EXIT_REJECTED, "sse_not_utf8") from exc
        if text == "":
            if data:
                if event_id is None or event_type is None:
                    raise FollowStop(EXIT_REJECTED, "sse_event_without_id")
                yield SseEvent(event_id, event_type, "\n".join(data))
            elif event_id is not None or event_type is not None:
                raise FollowStop(EXIT_REJECTED, "sse_event_without_data")
            data, size, event_id, event_type = [], 0, None, None
            continue
        if text.startswith(":"):
            continue
        name, sep, value = text.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            size += len(value.encode("utf-8")) + 1
            if size > max_event:
                raise FollowStop(EXIT_REJECTED, "sse_event_too_large")
            data.append(value)
        elif name == "id":
            if event_id is not None:
                raise FollowStop(EXIT_REJECTED, "sse_duplicate_field")
            event_id = value
        elif name == "event":
            if event_type is not None:
                raise FollowStop(EXIT_REJECTED, "sse_duplicate_field")
            event_type = value
        elif name == "retry":
            continue  # reconnection delay is local policy (bounded backoff)
        else:
            raise FollowStop(EXIT_REJECTED, "sse_unknown_field")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(_name):
    raise ValueError("non-finite number")


def decode_event(sse: SseEvent, plan_id: str) -> dict:
    """The SSE ``data`` as a T10 event of this plan whose ``id``/``event`` lines match the body."""
    try:
        event = json.loads(sse.data, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise FollowStop(EXIT_REJECTED, "event_not_json") from exc
    if not isinstance(event, dict):
        raise FollowStop(EXIT_REJECTED, "event_not_object")
    if event.get("plan_id") != plan_id:
        raise FollowStop(EXIT_REJECTED, "event_of_another_plan")
    if event.get("event_id") != sse.id or event.get("event_type") != sse.event:
        raise FollowStop(EXIT_REJECTED, "sse_header_mismatch")
    # T10 contract: a consumer stops on a schema version it does not know instead of guessing.
    if event.get("schema_version") not in PLATFORM_SCHEMA_VERSIONS:
        raise FollowStop(EXIT_REJECTED, "unsupported_schema_version")
    return event


def _t10_sequence(plan_id: str, doc: dict) -> int | None:
    if doc.get("source") != "platform.events":
        return None
    payload = doc.get("payload") or {}
    sequence = payload.get("sequence")
    if type(sequence) is not int or doc.get("origin_id") != f"t10:{plan_id}:{sequence}":
        raise StoreTampered("platform_record_unbound")
    return sequence


@dataclass
class PlanState:
    """What the sink holds for one plan, and the only path by which records are appended."""

    plan_id: str
    ledgers: dict[str, RunLedger]
    t10_runs: dict[int, str]  # T10 sequence → run that recorded it
    cursor_sequence: int  # Last-Event-ID sent to T10 (0 = from the beginning)
    expected: int = 0
    stored_last: int = 0  # last T10 sequence already in storage when this connection started
    written: int = 0
    absorbed: int = 0
    now: Callable[[], str] = field(default=utc_now)

    @classmethod
    def load(cls, plan_id: str, runs: dict[str, list[dict]], now: Callable[[], str] = utc_now) -> PlanState:
        t10: dict[int, str] = {}
        ledgers = {}
        for run, records in runs.items():
            ok, reason = verify_chain(records)
            if not ok:
                raise StoreTampered(reason)
            previous = 0
            for doc in records:
                sequence = _t10_sequence(plan_id, doc)
                if sequence is None:
                    continue
                if sequence in t10 or sequence <= previous:
                    raise StoreTampered("platform_sequence_repeated")
                t10[sequence], previous = run, sequence
            ledgers[run] = RunLedger.resume(plan_id, run, records, now)
        if sorted(t10) != list(range(1, len(t10) + 1)):
            raise StoreTampered("platform_sequence_gap")
        last = len(t10)
        # Always re-request the last stored event. T10 answers 409 only when the cursor row is
        # missing, so after a reset or a restore from another backup a new history at least as
        # long as ours would continue silently after it. The redelivery must be absorbed (same
        # run, same content under the same origin_id) or apply() stops with origin_conflict.
        # It also completes a split event (head + forwarded tool records) cut between writes.
        cursor = max(last - 1, 0)
        return cls(plan_id, ledgers, t10, cursor, expected=cursor + 1, stored_last=last, now=now)

    def last_event_id(self) -> str | None:
        return f"{self.plan_id}:{self.cursor_sequence}" if self.cursor_sequence else None

    def apply(self, event: dict, sink, on_receipt: Callable[[dict], None]) -> None:
        sequence = event.get("sequence")
        if type(sequence) is not int or sequence != self.expected:
            raise FollowStop(EXIT_REJECTED, "sequence_out_of_order")
        try:
            mapped = expand_platform_event(event)
        except ValueError as exc:
            raise FollowStop(EXIT_REJECTED, "event_rejected") from exc
        run = mapped[0][1]
        if self.t10_runs.get(sequence, run) != run:
            raise FollowStop(EXIT_REJECTED, "origin_conflict")
        ledger = self.ledgers.get(run)
        if ledger is None:
            ledger = self.ledgers[run] = RunLedger(self.plan_id, run, self.now)
        for _plan, _run, source, kind, origin_id, payload in mapped:
            try:
                record, created = ledger.append(source, kind, payload, origin_id)
            except OriginConflict as exc:
                raise FollowStop(EXIT_REJECTED, "origin_conflict") from exc
            except UnsafePayload as exc:
                raise FollowStop(EXIT_REJECTED, "unsafe_payload") from exc
            if not created:
                self.absorbed += 1
                continue
            try:
                receipt = sink.put(record).as_dict()
            except Exception as exc:  # noqa: BLE001 - any unconfirmed write stops the follower
                raise FollowStop(EXIT_WRITE, f"write_not_confirmed:{type(exc).__name__}") from exc
            self.written += 1
            on_receipt(receipt | {"t10_sequence": sequence})
        self.t10_runs[sequence] = run
        self.expected = sequence + 1

    def summary(self) -> dict:
        return {"last_t10_sequence": max(self.t10_runs, default=0),
                "runs": {run: ledger.chain_digest() for run, ledger in sorted(self.ledgers.items())}}


@dataclass(frozen=True)
class Upstream:
    """One T10 event stream endpoint. The credential is read from a file on every connection
    (rotation) and is never part of argv, logs or exceptions."""

    base_url: str
    plan_id: str
    token_file: Path | None = None
    header_name: str = "Authorization"
    scheme: str = "Bearer"
    ca_file: Path | None = None
    timeout: float = 60.0

    def __post_init__(self):
        url = urllib.parse.urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment \
                or url.username or url.password:
            raise FollowStop(EXIT_CONFIG, "invalid_base_url")
        if url.scheme == "http" and not _loopback(url.hostname):
            raise FollowStop(EXIT_CONFIG, "https_required")
        if not HEADER_TOKEN.match(self.header_name) or self.scheme and not HEADER_TOKEN.match(self.scheme):
            raise FollowStop(EXIT_CONFIG, "invalid_auth_header")
        if not IDENTIFIER.match(self.plan_id):
            raise FollowStop(EXIT_CONFIG, "invalid_plan_id")
        if self.timeout <= 0:
            raise FollowStop(EXIT_CONFIG, "invalid_timeout")

    def _credential(self) -> str | None:
        if self.token_file is None:
            return None
        try:
            raw = self.token_file.read_bytes()
        except OSError as exc:
            raise FollowStop(EXIT_CONFIG, "auth_token_unreadable") from exc
        token = raw.decode("ascii", "replace").strip()
        if not token or len(raw) > MAX_TOKEN_BYTES or not CREDENTIAL.match(token):
            raise FollowStop(EXIT_CONFIG, "auth_token_invalid")
        return f"{self.scheme} {token}" if self.scheme else token

    def connect(self, last_event_id: str | None, *, follow: bool):
        url = urllib.parse.urlsplit(self.base_url)
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-store"}
        credential = self._credential()
        if credential is not None:
            headers[self.header_name] = credential
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        path = f"{url.path.rstrip('/')}/v1/plans/{self.plan_id}/events?follow={'true' if follow else 'false'}"
        if url.scheme == "https":
            try:
                context = ssl.create_default_context(cafile=str(self.ca_file) if self.ca_file else None)
            except (OSError, ssl.SSLError) as exc:
                raise FollowStop(EXIT_CONFIG, "ca_file_unreadable") from exc
            conn = http.client.HTTPSConnection(url.hostname, url.port, timeout=self.timeout, context=context)
        else:
            conn = http.client.HTTPConnection(url.hostname, url.port, timeout=self.timeout)
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
        except ssl.SSLCertVerificationError as exc:
            conn.close()
            raise FollowStop(EXIT_CONFIG, "tls_verification_failed") from exc
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise Transient(f"connect_failed:{type(exc).__name__}") from exc
        if response.status == 200:
            if not response.getheader("Content-Type", "").startswith("text/event-stream"):
                conn.close()
                raise FollowStop(EXIT_REJECTED, "not_an_event_stream")
            return conn, response
        code = _error_code(response)
        conn.close()
        status = response.status
        if status in (401, 403):
            raise FollowStop(EXIT_ACCESS, "forbidden" if status == 403 else "unauthorized")
        if status == 404:
            raise FollowStop(EXIT_ACCESS, "plan_not_found")
        if status == 409:
            raise FollowStop(EXIT_RESYNC, "resync_required")
        if status == 400 and code == "invalid_cursor":
            raise FollowStop(EXIT_RESYNC, "invalid_cursor")
        if status == 400:
            raise FollowStop(EXIT_CONFIG, f"bad_request:{code}")
        if 300 <= status < 400:
            raise FollowStop(EXIT_CONFIG, "redirect_refused")  # never resend the credential elsewhere
        if status == 429 or status >= 500:
            raise Transient(f"upstream_status:{status}")
        raise FollowStop(EXIT_REJECTED, f"unexpected_status:{status}")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error_code(response) -> str:
    """The T10 error code label only; the body itself is never echoed."""
    try:
        body = json.loads(response.read(MAX_ERROR_BODY))
        code = body["detail"]["code"]
    except Exception:  # noqa: BLE001 - any unreadable body is just "unknown"
        return "unknown"
    return code if code in KNOWN_CODES else "unknown"


def _readline(response) -> Callable[[int], bytes]:
    def read(limit: int) -> bytes:
        try:
            return response.readline(limit)
        except (OSError, http.client.HTTPException) as exc:
            raise Transient(f"stream_broken:{type(exc).__name__}") from exc
    return read


def _read_store(load_runs: Callable[[], dict[str, list[dict]]]) -> dict[str, list[dict]]:
    """What the sink holds. Tampering and broken chains keep their own path; any other failure
    (S3 permission, throttling, network, local I/O) is reported by exception class only, since
    the message may name the bucket, key or path."""
    try:
        return load_runs()
    except (StoreTampered, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001 - botocore and OS errors share no base class
        raise StoreUnreadable(f"store_unreadable:{type(exc).__name__}") from None


def _backoff(attempt: int, max_backoff: float) -> float:
    delay = min(max_backoff, 2.0 ** min(attempt, 16))
    return delay / 2 + random.uniform(0, delay / 2)


def _log(**fields) -> None:
    print(json.dumps({"follow": fields.pop("event")} | fields, ensure_ascii=False), file=sys.stderr, flush=True)


def follow(upstream: Upstream, *, load_runs: Callable[[], dict[str, list[dict]]], sink, once: bool,
           on_receipt: Callable[[dict], None] = lambda _r: None, max_backoff: float = 30.0,
           sleep: Callable[[float], None] = time.sleep, now: Callable[[], str] = utc_now,
           max_connections: int | None = None) -> tuple[int, dict]:
    """Run until ``--once`` catches up, a non-retryable condition, or a stop signal.

    Returns (exit code, summary). Each connection starts from storage, so a reconnect after a
    broken stream and a restart after a crash take the same path."""
    attempt, connections = 0, 0
    totals = {"records": 0, "absorbed_redeliveries": 0}
    state: PlanState | None = None
    started_after: int | None = None

    def summary(code: int, reason: str) -> tuple[int, dict]:
        out = {"plan_id": upstream.plan_id, "mode": "once" if once else "continuous", "exit": code,
               "reason": reason, "resumed_after_sequence": started_after} | totals
        if state is not None:
            out |= state.summary()
        return code, out

    while True:
        try:
            state = PlanState.load(upstream.plan_id, _read_store(load_runs), now)
        except StoreUnreadable as exc:  # no resume point, so T10 is not read either
            if once:
                _log(event="stopped", exit=EXIT_STORE_UNREADABLE, reason=exc.reason)
                return summary(EXIT_STORE_UNREADABLE, exc.reason)
            delay = _backoff(attempt, max_backoff)
            attempt += 1
            _log(event="reconnect", reason=exc.reason, delay_s=round(delay, 2))
            sleep(delay)
            continue
        except StoreTampered as exc:
            _log(event="stopped", exit=EXIT_STORE, reason=f"stored_chain_untrusted:{exc}")
            return summary(EXIT_STORE, f"stored_chain_untrusted:{exc}")
        except ValueError as exc:  # RunLedger.resume refuses a broken or foreign chain
            _log(event="stopped", exit=EXIT_STORE, reason="stored_chain_untrusted")
            return summary(EXIT_STORE, f"stored_chain_untrusted:{type(exc).__name__}")
        if started_after is None:
            started_after = state.stored_last
        conn = None
        try:
            connections += 1
            conn, response = upstream.connect(state.last_event_id(), follow=not once)
            _log(event="connected", after_sequence=state.cursor_sequence)
            for sse in parse_sse(_readline(response)):
                state.apply(decode_event(sse, upstream.plan_id), sink, on_receipt)
                attempt = 0  # progress resets the backoff
            if not once:
                raise Transient("stream_ended")
            # T10 has no end-of-stream marker: after the headers a retention hole, a store error
            # or revoked access ends the stream exactly like "nothing more" does (uvicorn drops
            # the connection, readline() returns b""). So EOF is only "caught up" when the
            # connection brought nothing new; otherwise a fresh connection from storage either
            # finds nothing more or gets T10's real answer (409, 403, ...).
            delivered = state.expected - 1
            if delivered < state.stored_last:
                raise Transient("stream_ended_early")  # not even the re-requested event came
            totals["records"] += state.written
            totals["absorbed_redeliveries"] += state.absorbed
            if delivered == state.stored_last:
                return summary(EXIT_OK, "caught_up")
            if connections >= ONCE_MAX_CONNECTIONS:
                _log(event="stopped", exit=EXIT_UNAVAILABLE, reason="once_not_settled")
                return summary(EXIT_UNAVAILABLE, "once_not_settled")
            _log(event="confirm", after_sequence=delivered)
            continue
        except Transient as exc:
            totals["records"] += state.written
            totals["absorbed_redeliveries"] += state.absorbed
            if once:
                _log(event="stopped", exit=EXIT_UNAVAILABLE, reason=exc.reason)
                return summary(EXIT_UNAVAILABLE, exc.reason)
            if max_connections is not None and connections >= max_connections:
                return summary(EXIT_UNAVAILABLE, exc.reason)
            delay = _backoff(attempt, max_backoff)
            attempt += 1
            _log(event="reconnect", reason=exc.reason, delay_s=round(delay, 2))
            sleep(delay)
        except FollowStop as exc:
            totals["records"] += state.written
            totals["absorbed_redeliveries"] += state.absorbed
            _log(event="stopped", exit=exc.code, reason=exc.reason)
            return summary(exc.code, exc.reason)
        except KeyboardInterrupt:
            totals["records"] += state.written
            totals["absorbed_redeliveries"] += state.absorbed
            _log(event="stopped", exit=EXIT_OK, reason="signal")
            return summary(EXIT_OK, "signal")
        finally:
            if conn is not None:
                conn.close()


def cmd_follow(args) -> int:
    from .cli import _sink  # same sink construction as ``append``

    def receipts_writer():
        if not args.receipts:
            return lambda _r: None
        path = Path(args.receipts)

        def write(receipt: dict) -> None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return write

    try:
        upstream = Upstream(args.base_url, args.plan,
                            token_file=Path(args.auth_token_file) if args.auth_token_file else None,
                            header_name=args.auth_header, scheme=args.auth_scheme,
                            ca_file=Path(args.ca_file) if args.ca_file else None, timeout=args.read_timeout)
    except FollowStop as exc:
        _log(event="stopped", exit=exc.code, reason=exc.reason)
        return exc.code
    sink = _sink(args.sink, args.retention_days)
    client = getattr(sink, "client", None)

    def load_runs():
        return load_plan_runs(args.sink, args.plan, client)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        code, out = follow(upstream, load_runs=load_runs, sink=sink, once=args.once,
                           on_receipt=receipts_writer(), max_backoff=args.max_backoff)
    except KeyboardInterrupt:  # stopped while reading storage: nothing half written
        return EXIT_OK
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return code


def add_parser(sub) -> None:
    f = sub.add_parser("follow", help="subscribe to a T10 plan event stream and append it to the ledger")
    f.add_argument("--base-url", required=True, help="T10 API base URL (https; http only for loopback)")
    f.add_argument("--plan", required=True)
    f.add_argument("--sink", required=True, help="directory or s3://bucket[/prefix]; also the resume source")
    f.add_argument("--once", action="store_true", help="follow=false: record what exists, then exit")
    f.add_argument("--auth-token-file", default="", help="file holding the T10 read credential")
    f.add_argument("--auth-header", default="Authorization")
    f.add_argument("--auth-scheme", default="Bearer", help="prefix before the credential; empty for none")
    f.add_argument("--ca-file", default="", help="PEM bundle to verify the T10 server certificate")
    f.add_argument("--receipts", default="", help="append one JSON line per written record")
    f.add_argument("--retention-days", type=int, default=1)
    f.add_argument("--read-timeout", type=float, default=60.0)
    f.add_argument("--max-backoff", type=float, default=30.0)
    f.set_defaults(func=cmd_follow)
