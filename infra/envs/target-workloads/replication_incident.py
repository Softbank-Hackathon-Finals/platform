#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.6"]
# ///
"""Say when the forward subscription stopped, why, and how far behind it is. Reads only; changes nothing.

    kubectl -n target-dev logs target-db-1 -c postgres --since=48h | infra/envs/target-workloads/replication_incident.py

The catalog keeps no time or reason for a stop, so those come from the target instance's log on stdin.
A log line can hold row values and addresses, so only log times, SQLSTATE codes and fixed labels are
printed; message text never is. The gap and error counts use the resume command's read, and need its
two DSNs in the environment. Without them the log part still prints. Exit 0 means both parts were read.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().with_name("testing")))

from lag_metrics import LOG_EVENTS, parse_lsn

SOURCE_DSN_ENV = "T8_RESUME_SOURCE_DSN"
TARGET_DSN_ENV = "T8_RESUME_TARGET_DSN"
DISABLED = "subscription_disabled"
OTHER_ERROR = "other_worker_error"
ADMIN_SHUTDOWN = "57P01"  # A pod restart or planned stop, which must not bury the failure being looked for.
CONNECTION_EVENTS = {"receiver_timeout", "stream_lost", "publisher_connect_failed", "slot_held_by_another_process"}
STATE_FIELDS = {"target": ("forward_enabled", "apply_workers", "disable_on_error", "target_apply_errors",
                           "target_sync_errors", "target_conflicts"),
                "source": ("source_observed_at", "source_slot_active", "source_slot_wal_status", "source_slot_invalidated")}
MIB = 1024 * 1024


def cause_of(event, sqlstate):
    """What kind of failure this is. Why a link broke is outside what a log can tell."""
    if event == "apply_conflict" or sqlstate.startswith("23"):
        return "conflict"
    if event in CONNECTION_EVENTS or sqlstate.startswith("08"):
        return "connection"
    return "other"


def label_of(record):
    """A fixed label, or None for what is not a subscription worker's failure."""
    if not record.get("backend_type", "").startswith("logical replication") or record.get("sql_state_code") == ADMIN_SHUTDOWN:
        return None
    message = record.get("message", "")
    label = next((name for text, name in LOG_EVENTS.items() if text in message), None)
    return label or (OTHER_ERROR if record.get("error_severity") in ("ERROR", "FATAL") else None)


def read_log(lines):
    """Fold one instance's log into times, labels and codes. Each stop is tied to its own worker's last error."""
    report = {"postgres_records": 0, "unparsed_lines": 0, "log_window": {"first": None, "last": None},
              "disables": [], "latest_cause": None, "latest_error": None, "counts": {}}
    last_error_of_process = {}
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            report["unparsed_lines"] += 1
            continue
        record = parsed.get("record") if isinstance(parsed, dict) and parsed.get("logger") == "postgres" else None
        if not isinstance(record, dict):
            continue
        at = record.get("log_time")
        report["postgres_records"] += 1
        report["log_window"]["first"] = report["log_window"]["first"] or at
        report["log_window"]["last"] = at
        label = label_of(record)
        if label is None:
            continue
        if label == DISABLED:
            error = last_error_of_process.pop(record.get("process_id"), None)
            report["disables"].append({"at": at, "cause": "unknown", "event": None, "sqlstate": None} if error is None
                                      else {**error, "at": at})
            report["latest_cause"] = report["disables"][-1]["cause"]
            continue
        report["counts"][label] = report["counts"].get(label, 0) + 1
        if record.get("error_severity") in ("ERROR", "FATAL"):
            sqlstate = record.get("sql_state_code", "")
            error = {"at": at, "cause": cause_of(label, sqlstate), "event": label, "sqlstate": sqlstate}
            last_error_of_process[record.get("process_id")] = error
            report["latest_error"] = error
    return report


def state_view(source, target):
    """Whichever side could be read. Either may be None: in a link failure the source is what cannot be reached."""
    flush = source and source["source_wal_flush_lsn"]
    confirmed = source and source["source_slot_confirmed_flush_lsn"]
    gap = parse_lsn(flush) - parse_lsn(confirmed) if flush and confirmed else None
    return {**{name: target[name] if target else None for name in STATE_FIELDS["target"]},
            **{name: source[name] if source else None for name in STATE_FIELDS["source"]},
            "gap_bytes": gap, "gap_mib": None if gap is None else round(gap / MIB, 1)}


def read_state(environ, read_source, read_target):
    missing = [name for name in (SOURCE_DSN_ENV, TARGET_DSN_ENV) if not environ.get(name)]
    if missing:
        return {"read": False, "missing": missing}
    # Imported here so that the log part needs no driver and no connection.
    import resume_replication as resume

    read_source = read_source or (lambda dsn: resume.read_one(dsn, "source_status", resume.NAMES))
    read_target = read_target or resume.read_target
    values, errors = {"source": None, "target": None}, {}
    for side, read, dsn in (("target", read_target, environ[TARGET_DSN_ENV]), ("source", read_source, environ[SOURCE_DSN_ENV])):
        try:
            values[side] = read(dsn)
        except Exception as error:  # noqa: BLE001 - Driver messages can carry a host or DSN; only the class is reported.
            errors[side] = resume.describe(error)
    return {"read": not errors, **({"errors": errors} if errors else {}), **state_view(values["source"], values["target"])}


def main(environ, read_source=None, read_target=None):
    report = read_log(sys.stdin.read().splitlines())
    report["state"] = read_state(environ, read_source, read_target)
    print(json.dumps(report, indent=2))
    return 0 if report["postgres_records"] and report["state"]["read"] else 1


if __name__ == "__main__":
    raise SystemExit(main(os.environ))
