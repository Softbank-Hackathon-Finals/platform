"""Shared event operations with a SQLite reference transaction boundary."""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .contract import MAX_SEQUENCE, validate_event, validate_identifier


class EventError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EventStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS streams (
                    plan_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS state_owners (
                    plan_id TEXT PRIMARY KEY REFERENCES streams(plan_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    plan_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    producer_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (plan_id, sequence), UNIQUE (plan_id, producer_key),
                    FOREIGN KEY (plan_id) REFERENCES streams(plan_id)
                );
            """)

    @contextmanager
    def _connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    @contextmanager
    def _transaction(self, *, write: bool = False, plan_id: str | None = None):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c

    def register_stream(self, plan_id: str) -> None:
        """The owning API must validate plan existence and authorization first."""
        validate_identifier(plan_id)
        with self._transaction(write=True, plan_id=plan_id) as c:
            c.execute("INSERT INTO streams(plan_id) VALUES (?) ON CONFLICT(plan_id) DO NOTHING", (plan_id,))

    def append(
        self,
        *,
        plan_id: str,
        run_id: str,
        event_type: str,
        data: dict,
        occurred_at: str,
        producer_key: str,
    ) -> dict:
        """Same plan/key/input replays the same event; changed input is rejected."""
        with self._transaction(write=True, plan_id=plan_id) as c:
            return self._append_in_transaction(
                c,
                plan_id=plan_id,
                run_id=run_id,
                event_type=event_type,
                data=data,
                occurred_at=occurred_at,
                producer_key=producer_key,
            )

    def _append_in_transaction(
        self,
        c,
        *,
        plan_id: str,
        run_id: str,
        event_type: str,
        data: dict,
        occurred_at: str,
        producer_key: str,
        state_owner: bool = False,
    ) -> dict:
        """Internal owner port: caller holds the shared write transaction."""
        validate_identifier(plan_id)
        validate_identifier(run_id)
        validate_identifier(producer_key)
        request = dict(
            plan_id=plan_id,
            run_id=run_id,
            event_type=event_type,
            data=data,
            occurred_at=occurred_at,
        )
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(canonical.encode()) > 32768:
            raise EventError("event_too_large")
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        managed = not state_owner and c.execute(
            "SELECT 1 FROM state_owners WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if managed and event_type == "state.changed":
            raise EventError("managed_state_required")
        # T20: only the FAILBACK adapter records failback approvals and progress for a managed plan,
        # so a request visible on SSE is always the one its decision names.
        if managed and (
            event_type == "failback.recorded"
            or (event_type in ("approval.requested", "approval.decided")
                and isinstance(data, dict) and data.get("approval_kind") == "failback")
        ):
            raise EventError("managed_failback_required")
        stream = c.execute(
            "SELECT next_sequence FROM streams WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if stream is None:
            raise EventError("plan_not_found")
        old = c.execute(
            "SELECT * FROM events WHERE plan_id=? AND producer_key=?", (plan_id, producer_key)
        ).fetchone()
        if old:
            if old["request_hash"] != fingerprint:
                raise EventError("producer_key_conflict")
            return json.loads(old["event_json"])
        sequence = stream["next_sequence"]
        if sequence > MAX_SEQUENCE:
            raise EventError("sequence_exhausted")
        event = {
            "schema_version": "1.0",
            "event_id": f"{plan_id}:{sequence}",
            "sequence": sequence,
            **request,
        }
        validate_event(event)
        c.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (
                plan_id,
                sequence,
                producer_key,
                fingerprint,
                json.dumps(event, ensure_ascii=False),
            ),
        )
        c.execute("UPDATE streams SET next_sequence=? WHERE plan_id=?", (sequence + 1, plan_id))
        return event

    def read_after(self, plan_id: str, cursor: str | None, *, limit: int = 200) -> list[dict]:
        validate_identifier(plan_id)
        if not 1 <= limit <= 1000:
            raise ValueError("invalid_limit")
        sequence = 0
        if cursor:
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_-]{0,127}):([1-9][0-9]{0,15})", cursor)
            if not match or match[1] != plan_id or int(match[2]) > MAX_SEQUENCE:
                raise EventError("invalid_cursor")
            sequence = int(match[2])
        with self._transaction() as c:
            stream = c.execute(
                "SELECT next_sequence FROM streams WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if stream is None:
                raise EventError("plan_not_found")
            if (
                sequence
                and c.execute(
                    "SELECT 1 FROM events WHERE plan_id=? AND sequence=?", (plan_id, sequence)
                ).fetchone()
                is None
            ):
                raise EventError("resync_required")
            rows = c.execute(
                "SELECT sequence,event_json FROM events WHERE plan_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (plan_id, sequence, limit),
            ).fetchall()
            # Deleting retained rows must not silently create a gap, including an initial replay.
            if rows and rows[0]["sequence"] != sequence + 1:
                raise EventError("resync_required")
            if not rows and sequence + 1 < stream["next_sequence"]:
                raise EventError("resync_required")
            if rows and len(rows) < limit and rows[-1]["sequence"] + 1 < stream["next_sequence"]:
                raise EventError("resync_required")
            for previous, current in zip(rows, rows[1:], strict=False):
                if current["sequence"] != previous["sequence"] + 1:
                    raise EventError("resync_required")
            result = [json.loads(row["event_json"]) for row in rows]
            for event in result:
                validate_event(event)
            return result
