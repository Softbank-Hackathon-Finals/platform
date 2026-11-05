"""Signed rehearsal.exec records: written per action, merged into the attempt record.

Each action leaves a write-once `before` record (seq, action, checked arguments,
argument digest) before it runs and an `after` record (result, reason, output
digest and size) when it ends. A `before` without `after` is an action whose
effect is unknown, also after a restart. The merged list is the attempt
record's `exec_events`; any doubt about it makes the whole list UNAVAILABLE,
the same rule as `collect_tool_events`.
"""

from dataclasses import dataclass, field

from .exec_contract import PROBE_OUTCOMES, REASONS, RESULT_CODES, STAGES

_COMMON = {"run_id", "attempt", "seq", "action", "request_id", "args_sha256", "result_code", "reason",
           "stage", "output_sha256", "output_bytes"}
KEYS = {"apply": _COMMON | {"proposal_digest"},
        "inspect": _COMMON | {"target_kind", "target_name", "lines"},
        "probe": _COMMON | {"service", "port", "method", "path", "probe_outcome", "http_status"}}
UNKNOWN = {"result_code": "UNKNOWN", "reason": "EFFECT_UNKNOWN", "stage": "EXECUTE"}


@dataclass(frozen=True)
class StoreJournal:
    """`exec_session.Journal` over the attempt store of one admitted attempt."""

    store: object = field(repr=False)
    admission: object

    def write(self, seq: int, phase: str, body: dict) -> None:
        self.store.write_exec(self.admission, seq, phase, body)


def _valid(event: dict, admission: object) -> bool:
    action, code, reason = event.get("action"), event.get("result_code"), event.get("reason")
    if action not in KEYS or not set(event) <= KEYS[action]:
        return False
    if any(type(v) not in (str, int, bool) for v in event.values()):
        return False
    if event.get("run_id") != admission.rehearsal_id or event.get("attempt") != admission.attempt:
        return False
    if code not in RESULT_CODES or ("reason" in event) != ("stage" in event):
        return False
    if reason is not None and (reason not in REASONS or event["stage"] not in STAGES):
        return False
    if (code == "OK") != (reason in (None, "OUTPUT_WITHHELD")):
        return False
    return event.get("probe_outcome", "CONNECTED") in PROBE_OUTCOMES


def merge_exec_events(records: dict[tuple[int, str], dict], admission: object) -> list[dict]:
    """Merge before/after records in seq order; raise ValueError on gaps or foreign values."""
    seqs = sorted({seq for seq, _ in records})
    if seqs != list(range(1, len(seqs) + 1)) or any((seq, "before") not in records for seq in seqs):
        raise ValueError("exec records are not contiguous")
    events = []
    for seq in seqs:
        before, after = records[(seq, "before")], records.get((seq, "after"))
        if before.get("seq") != seq or (after is not None and set(after) & set(before)):
            raise ValueError("exec record mismatch")
        event = before | (after if after is not None else UNKNOWN)
        if not _valid(event, admission):
            raise ValueError("exec record outside the contract")
        events.append(event)
    return events


def collect_exec_record(store: object, admission: object, phase: object) -> dict:
    """Attempt-record keys for an attempt that ran with an exec phase."""
    trace = phase.traces[-1] if getattr(phase, "traces", None) else None
    body: dict = {}
    if trace is not None and trace.space is not None:
        body["exec_space"] = {"namespace": trace.space.name, "created_uid": trace.space.uid,
                              "absence_confirmed": trace.removed is True}
    try:
        if trace is None or trace.journal_failed:
            raise ValueError("exec journal incomplete")
        events = merge_exec_events(store.read_exec(admission), admission)
    except Exception:
        return body | {"exec_events_status": "UNAVAILABLE"}
    return body | {"exec_events_status": "COMPLETE", "exec_events": events}


__all__ = ["KEYS", "StoreJournal", "collect_exec_record", "merge_exec_events"]
