"""The read-only incident report: when the forward subscription stopped, why, and how far behind it is."""
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR / "testing"))
incident = importlib.import_module("replication_incident")

# Row values and addresses reach a real log through these fields. None of them may reach the report.
ROW_VALUE = "row-value-marker"
ADDRESS = "address-marker"
APPLY_WORKER = "logical replication apply worker"


def record(at, severity, sqlstate, message, *, pid="77", backend=APPLY_WORKER, **fields):
    """One line as `kubectl logs` prints it for a CNPG instance."""
    return json.dumps({"level": "info", "ts": "2026-11-05T14:50:00Z", "logger": "postgres", "msg": "record",
                       "logging_pod": "target-db-1",
                       "record": {"log_time": at, "process_id": pid, "error_severity": severity, "sql_state_code": sqlstate,
                                  "message": message, "backend_type": backend, **fields}})


TIMEOUT = record("2026-11-05 14:50:01.100 UTC", "ERROR", "08006", "terminating logical replication worker due to timeout")
STREAM_LOST = record("2026-11-05 14:50:01.100 UTC", "ERROR", "08006",
                     f"could not receive data from WAL stream: connection to server at {ADDRESS} lost")
DISABLED = record("2026-11-05 14:50:01.118 UTC", "LOG", "00000", 'subscription "t8_source_to_target" has been disabled because of an error')
CONFLICT = record("2026-11-05 14:51:00.000 UTC", "ERROR", "23505", 'conflict detected on relation "public.messages": conflict=insert_exists',
                  detail=f"Key (id)=({ROW_VALUE}) already exists.")
CONFLICT_DISABLED = record("2026-11-05 14:51:00.020 UTC", "LOG", "00000", 'subscription "t8_source_to_target" has been disabled because of an error')
OTHER_ERROR = record("2026-11-05 14:52:00.000 UTC", "ERROR", "42703", f'column "{ROW_VALUE}" does not exist')
OTHER_DISABLED = record("2026-11-05 14:52:00.010 UTC", "LOG", "00000", 'subscription "t8_source_to_target" has been disabled because of an error')


def report_of(*lines):
    return incident.read_log(list(lines))


class CauseTests(unittest.TestCase):
    def test_timeout_is_a_connection_cause_with_its_time_and_code(self):
        report = report_of(TIMEOUT, DISABLED)
        self.assertEqual(report["disables"], [{"at": "2026-11-05 14:50:01.118 UTC", "cause": "connection",
                                               "event": "receiver_timeout", "sqlstate": "08006"}])
        self.assertEqual(report["latest_cause"], "connection")

    def test_lost_stream_is_a_connection_cause_too(self):
        self.assertEqual(report_of(STREAM_LOST, DISABLED)["disables"][0]["event"], "stream_lost")

    def test_conflict_is_told_apart_from_a_connection_failure(self):
        report = report_of(TIMEOUT, DISABLED, CONFLICT, CONFLICT_DISABLED)
        self.assertEqual([item["cause"] for item in report["disables"]], ["connection", "conflict"])
        self.assertEqual(report["latest_cause"], "conflict")

    def test_any_other_worker_error_keeps_only_its_code(self):
        report = report_of(OTHER_ERROR, OTHER_DISABLED)
        self.assertEqual(report["disables"], [{"at": "2026-11-05 14:52:00.010 UTC", "cause": "other",
                                               "event": "other_worker_error", "sqlstate": "42703"}])

    def test_a_disable_without_its_error_in_the_log_is_unknown_not_a_guess(self):
        report = report_of(DISABLED)
        self.assertEqual(report["disables"][0]["cause"], "unknown")
        self.assertEqual(report["latest_cause"], "unknown")

    def test_another_process_error_is_not_taken_for_the_cause(self):
        other_worker = record("2026-11-05 14:50:01.000 UTC", "ERROR", "23505",
                              'conflict detected on relation "public.messages": conflict=insert_exists', pid="99")
        self.assertEqual(report_of(other_worker, DISABLED)["disables"][0]["cause"], "unknown")

    def test_a_logged_conflict_that_did_not_stop_the_worker_is_counted_but_is_not_the_cause(self):
        # An update on a missing row is only logged, and replication carries on.
        logged = record("2026-11-05 14:50:00.000 UTC", "LOG", "23000", 'conflict detected on relation "public.messages": conflict=update_missing')
        report = report_of(logged, DISABLED)
        self.assertEqual(report["counts"]["apply_conflict"], 1)
        self.assertIsNone(report["latest_error"])
        self.assertEqual(report["disables"][0]["cause"], "unknown")

    def test_repeated_errors_without_a_disable_are_counted_as_retrying(self):
        # With disable_on_error off a conflict never reaches the "disabled" line; only the count tells.
        report = report_of(CONFLICT, CONFLICT, CONFLICT)
        self.assertEqual(report["disables"], [])
        self.assertEqual(report["latest_cause"], None)
        self.assertEqual(report["counts"], {"apply_conflict": 3})
        self.assertEqual(report["latest_error"], {"at": "2026-11-05 14:51:00.000 UTC", "cause": "conflict",
                                                  "event": "apply_conflict", "sqlstate": "23505"})


class EvidenceWindowTests(unittest.TestCase):
    def test_the_log_window_is_stated_so_an_empty_result_is_not_read_as_healthy(self):
        start = record("2026-11-05 10:00:00.000 UTC", "LOG", "00000", "checkpoint complete", backend="checkpointer")
        report = report_of(start, TIMEOUT, DISABLED)
        self.assertEqual(report["log_window"], {"first": "2026-11-05 10:00:00.000 UTC", "last": "2026-11-05 14:50:01.118 UTC"})
        self.assertEqual(report["postgres_records"], 3)

    def test_no_postgres_record_means_nothing_can_be_said(self):
        report = report_of("not json", json.dumps({"logger": "instance-manager", "msg": "starting"}))
        self.assertEqual(report["postgres_records"], 0)
        self.assertEqual(report["unparsed_lines"], 1)
        self.assertEqual(report["log_window"], {"first": None, "last": None})
        self.assertIsNone(report["latest_cause"])

    def test_events_of_ordinary_backends_stay_out(self):
        session_error = record("2026-11-05 14:50:00.000 UTC", "ERROR", "23505", f"duplicate key {ROW_VALUE}", backend="client backend")
        self.assertEqual(report_of(session_error)["counts"], {})


class NoValueLeavesTests(unittest.TestCase):
    def test_no_log_text_reaches_the_report(self):
        secret_fields = {"detail": ROW_VALUE, "query": ROW_VALUE, "context": ROW_VALUE, "connection_from": ADDRESS,
                         "application_name": ADDRESS, "user_name": ROW_VALUE}
        lines = [record("2026-11-05 14:50:01.100 UTC", "ERROR", "08006",
                        f"could not receive data from WAL stream: {ADDRESS} {ROW_VALUE}", **secret_fields),
                 record("2026-11-05 14:50:01.118 UTC", "LOG", "00000", 'subscription "t8_source_to_target" has been disabled because of an error',
                        **secret_fields),
                 OTHER_ERROR]
        printed = json.dumps(report_of(*lines))
        self.assertNotIn(ROW_VALUE, printed)
        self.assertNotIn(ADDRESS, printed)


SOURCE_STATE = {"source_observed_at": "2026-11-05T14:55:00Z", "source_wal_flush_lsn": "0/1C000000",
                "source_slot_confirmed_flush_lsn": "0/16000000", "source_slot_active": False, "source_slot_wal_status": "reserved",
                "source_slot_invalidated": False}
TARGET_STATE = {"forward_enabled": False, "apply_workers": 0, "disable_on_error": True, "target_apply_errors": 3,
                "target_sync_errors": 0, "target_conflicts": 0, "system_identifier": "7000000000000000002"}


class StateTests(unittest.TestCase):
    def run_main(self, lines, environ, read=None):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("\n".join(lines))), patch.object(sys, "stdout", output):
            code = incident.main(environ, read_status=read or (lambda *_: {"source": SOURCE_STATE, "target": TARGET_STATE}))
        return code, json.loads(output.getvalue())

    def test_gap_and_counts_come_from_the_resume_commands_read(self):
        code, report = self.run_main([TIMEOUT, DISABLED], {"T8_RESUME_SOURCE_DSN": "s", "T8_RESUME_TARGET_DSN": "t"})
        self.assertEqual(code, 0)
        state = report["state"]
        self.assertEqual(state["gap_bytes"], 100_663_296)
        self.assertEqual(state["gap_mib"], 96.0)
        self.assertEqual((state["forward_enabled"], state["apply_workers"], state["target_apply_errors"]), (False, 0, 3))
        self.assertNotIn("system_identifier", state)

    def test_without_connections_the_log_part_still_prints_and_the_exit_says_incomplete(self):
        code, report = self.run_main([TIMEOUT, DISABLED], {})
        self.assertEqual(code, 1)
        self.assertEqual(report["latest_cause"], "connection")
        self.assertEqual(report["state"], {"read": False, "missing": ["T8_RESUME_SOURCE_DSN", "T8_RESUME_TARGET_DSN"]})

    def test_a_failed_read_reports_only_the_class_name(self):
        def fail(*_):
            raise OSError(f"could not reach {ADDRESS}")

        code, report = self.run_main([TIMEOUT, DISABLED], {"T8_RESUME_SOURCE_DSN": "s", "T8_RESUME_TARGET_DSN": "t"}, read=fail)
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], {"read": False, "error": "OSError"})
        self.assertNotIn(ADDRESS, json.dumps(report))

    def test_an_empty_log_exits_one_even_when_the_state_was_read(self):
        code, _ = self.run_main([], {"T8_RESUME_SOURCE_DSN": "s", "T8_RESUME_TARGET_DSN": "t"})
        self.assertEqual(code, 1)

    def test_a_missing_slot_gives_no_gap_rather_than_a_crash(self):
        source = {**SOURCE_STATE, "source_slot_confirmed_flush_lsn": None}
        code, report = self.run_main([TIMEOUT, DISABLED], {"T8_RESUME_SOURCE_DSN": "s", "T8_RESUME_TARGET_DSN": "t"},
                                     read=lambda *_: {"source": source, "target": TARGET_STATE})
        self.assertEqual(code, 0)
        self.assertIsNone(report["state"]["gap_bytes"])


if __name__ == "__main__":
    unittest.main()
