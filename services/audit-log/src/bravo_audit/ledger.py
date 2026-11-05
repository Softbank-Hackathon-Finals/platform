"""Per-run hash chain. Same inputs in the same order always produce the same records."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .record import GENESIS, AuditRecord, canonical, check_payload, sha256


class OriginConflict(ValueError):
    """The same upstream event identity arrived again with different content."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunLedger:
    def __init__(self, plan_id: str, run_id: str, now: Callable[[], str] = utc_now, prev_sha256: str = GENESIS, start_sequence: int = 1):
        self.plan_id, self.run_id, self.now = plan_id, run_id, now
        self.prev_sha256 = prev_sha256
        self.next_sequence = start_sequence
        self.records: list[AuditRecord] = []
        # upstream identity → payload digest, including records stored before a resume
        self.seen: dict[str, str] = {}

    def append(self, source: str, kind: str, payload: dict, origin_id: str) -> tuple[AuditRecord | None, bool]:
        """Append one upstream event. Returns (record, created).

        At-least-once delivery (T10 contract) means the same ``origin_id`` may arrive again: an
        identical replay is absorbed (``created=False``, no new record), a different payload under
        the same identity is a conflict and is refused."""
        digest = sha256(check_payload(payload))
        if origin_id in self.seen:
            if self.seen[origin_id] != digest:
                raise OriginConflict("same upstream event id with different content")
            return next((r for r in self.records if r.origin_id == origin_id), None), False
        record = AuditRecord.build(plan_id=self.plan_id, run_id=self.run_id, sequence=self.next_sequence,
                                   recorded_at=self.now(), source=source, kind=kind, origin_id=origin_id,
                                   payload=payload, prev_sha256=self.prev_sha256)
        self.records.append(record)
        self.seen[origin_id] = record.payload_sha256
        self.prev_sha256 = record.sha256
        self.next_sequence += 1
        return record, True

    @classmethod
    def resume(cls, plan_id: str, run_id: str, existing: list[dict], now: Callable[[], str] = utc_now) -> RunLedger:
        """Continue a chain whose earlier records were already stored (and verified)."""
        ok, reason = verify_chain(existing)
        if not ok:
            raise ValueError(f"cannot resume a broken chain: {reason}")
        if existing and (existing[0]["plan_id"] != plan_id or existing[0]["run_id"] != run_id):
            raise ValueError("cannot resume another run's chain")
        last = existing[-1] if existing else None
        ledger = cls(plan_id, run_id, now, prev_sha256=last["sha256"] if last else GENESIS,
                     start_sequence=(last["sequence"] + 1) if last else 1)
        ledger.seen = {doc["origin_id"]: doc["payload_sha256"] for doc in existing}
        return ledger


def verify_chain(records: list[dict]) -> tuple[bool, str]:
    """Return (True, "ok") or (False, reason). Reasons are code-owned labels, never record text."""
    from .record import validate_record

    prev, expected = GENESIS, 1
    run = None
    for doc in records:
        try:
            validate_record(doc)
        except Exception:  # noqa: BLE001 - any schema/hash failure is one verdict
            return False, f"invalid_record:{expected}"
        if run is None:
            run = (doc["plan_id"], doc["run_id"])
        elif run != (doc["plan_id"], doc["run_id"]):
            return False, f"mixed_runs:{expected}"
        if doc["sequence"] != expected:
            return False, f"sequence_gap:{expected}"
        if doc["prev_sha256"] != prev:
            return False, f"chain_broken:{expected}"
        prev, expected = doc["sha256"], expected + 1
    return True, "ok"


def chain_digest(records: list[dict]) -> str:
    """One value that identifies the whole chain (last sha256, or genesis for an empty run)."""
    return records[-1]["sha256"] if records else sha256(canonical([]))
