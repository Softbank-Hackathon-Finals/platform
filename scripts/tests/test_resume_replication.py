"""Refusal rules, post-check verdicts and credential handling of the forward-subscription resume command."""

import copy
import importlib
import io
import json
import re
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR / "testing"))
resume = importlib.import_module("resume_replication")
lab = importlib.import_module("lag_lab")
metrics = importlib.import_module("lag_metrics")

SECRET = "t8-test-password-marker"
ENVIRON = {"T8_RESUME_SOURCE_DSN": f"host=source.invalid password={SECRET}",
           "T8_RESUME_TARGET_DSN": f"host=target.invalid password={SECRET}",
           "T8_RESUME_EXECUTOR_DSN": f"host=state.invalid password={SECRET}",
           "T8_RESUME_SOURCE_PROBE_DSN": f"host=probe.invalid password={SECRET}"}
SOURCE_ID, TARGET_ID = "7000000000000000001", "7000000000000000002"
PINS = ("--source-system-id", SOURCE_ID, "--target-system-id", TARGET_ID)


def healthy():
    """The state reported by the actual incident: the forward subscription off, everything else as designed."""
    return {
        "source": {
            "system_identifier": SOURCE_ID, "barrier_published": True,
            "in_recovery": False, "app_can_login": True, "control_sessions": 0,
            "wal_sender_timeout": "5s", "max_slot_wal_keep_size": "1GB",
            "slot": {"type": "logical", "active": False, "wal_status": "reserved", "safe_wal_size": 900,
                     "invalidation_reason": None, "confirmed_flush_lsn": "0/1000000"},
            "reverse": {"enabled": False, "workers": 0},
        },
        "target": {
            "system_identifier": TARGET_ID,
            "in_recovery": False, "app_can_login": False, "app_sessions": 0, "control_sessions": 0,
            "wal_receiver_timeout": "5s", "reverse_slot_active": False,
            "subscription": {"enabled": False, "disable_on_error": True, "tables_not_ready": 0,
                             "barrier_subscribed": True, "barrier_readable": True, "worker_pid": None,
                             "received_lsn": None, "latest_end_lsn": None, "apply_error_count": 1,
                             "sync_error_count": 0, "conflicts": 0, "can_alter": True, "can_call_control": False},
        },
        "source_rows": {"messages": 10, "messages_max_id": 10, "attachments": 2, "wal_lsn": "0/6000000"},
        "target_rows": {"messages": 8, "messages_max_id": 8, "attachments": 2},
        "executor": {"state": "idle", "holders": 0},
        "probe_allowed": True,
    }


def changed(path, value):
    state = healthy()
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return state


def caught_up():
    """What a read shows once the target has confirmed everything up to the watermark."""
    state = healthy()
    state["target"]["subscription"].update(enabled=True, worker_pid=4242, received_lsn="0/6000100", latest_end_lsn="0/6000100")
    state["source"]["slot"].update(active=True, confirmed_flush_lsn="0/6000100")
    state["target_rows"] = {"messages": 10, "messages_max_id": 10, "attachments": 2}
    return state


class RefusalTests(unittest.TestCase):
    def test_the_reported_incident_state_passes(self):
        self.assertEqual(resume.refusals(healthy()), [])

    def test_each_unsafe_state_is_refused_for_its_own_reason(self):
        cases = {
            "slot_missing": changed(["source", "slot"], None),
            "slot_invalidated": changed(["source", "slot", "invalidation_reason"], "wal_removed"),
            "slot_at_risk": changed(["source", "slot", "wal_status"], "unreserved"),
            "slot_not_logical": changed(["source", "slot", "type"], "physical"),
            "slot_in_use": changed(["source", "slot", "active"], True),
            "subscription_missing": changed(["target", "subscription"], None),
            "reverse_active": changed(["source", "reverse", "enabled"], True),
            "reverse_slot_in_use": changed(["target", "reverse_slot_active"], True),
            "target_app_can_login": changed(["target", "app_can_login"], True),
            "target_app_sessions": changed(["target", "app_sessions"], 1),
            "source_app_cannot_login": changed(["source", "app_can_login"], False),
            "source_is_not_primary": changed(["source", "in_recovery"], True),
            "target_is_not_primary": changed(["target", "in_recovery"], True),
            "executor_running": changed(["executor"], {"state": "holding", "holders": 1}),
            "executor_state_unconfirmed": changed(["executor"], {"state": "unconfirmed"}),
            "control_session_present": changed(["target", "control_sessions"], 1),
            "target_ahead_of_source": changed(["target_rows", "messages_max_id"], 11),
            "source_and_target_are_one_cluster": changed(["target", "system_identifier"], SOURCE_ID),
        }
        for reason, state in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, resume.refusals(state))

    def test_a_running_reverse_worker_counts_even_if_the_flag_says_off(self):
        state = changed(["source", "reverse"], {"enabled": False, "workers": 1})
        self.assertIn("reverse_active", resume.refusals(state))

    def test_lost_wal_status_is_invalidated_without_a_reason_column(self):
        state = changed(["source", "slot", "wal_status"], "lost")
        self.assertIn("slot_invalidated", resume.refusals(state))

    def test_a_role_that_cannot_be_read_is_not_a_safe_role(self):
        self.assertIn("target_app_can_login", resume.refusals(changed(["target", "app_can_login"], None)))
        self.assertIn("source_app_cannot_login", resume.refusals(changed(["source", "app_can_login"], None)))

    def test_no_reverse_subscription_is_off_rather_than_unknown(self):
        self.assertEqual(resume.refusals(changed(["source", "reverse"], None)), [])

    def test_the_slot_is_expected_to_be_held_once_the_subscription_runs(self):
        self.assertEqual(resume.refusals(caught_up()), [])

    def test_no_privilege_to_enable_is_refused_only_when_enabling_is_needed(self):
        state = healthy()
        state["target"]["subscription"]["can_alter"] = False
        self.assertIn("no_enable_privilege", resume.refusals(state))
        running = caught_up()
        running["target"]["subscription"]["can_alter"] = False
        self.assertEqual(resume.refusals(running), [])

    def test_a_barrier_outside_the_publication_only_stops_the_probe(self):
        for path, reason in ((["source", "barrier_published"], "barrier_not_published"),
                             (["target", "subscription", "barrier_subscribed"], "barrier_not_subscribed"),
                             (["target", "subscription", "barrier_readable"], "barrier_not_readable_on_target"),
                             (["probe_allowed"], "barrier_probe_not_permitted")):
            with self.subTest(reason=reason):
                state = changed(path, False)
                self.assertEqual(resume.refusals(state), [])
                self.assertIn(reason, resume.refusals(state, probe=True))

    def test_a_probe_role_that_could_not_be_read_is_not_permitted(self):
        state = healthy()
        state["probe_allowed"] = None
        self.assertIn("barrier_probe_not_permitted", resume.refusals(state, probe=True))

    def test_lease_statuses_match_the_executor_contract(self):
        contract = (ROOT / "services/cutover-executor/src/bravo_cutover/contract.py").read_text()
        declared = re.search(r"LEASE_HOLDING = \(([^)]*)\)", contract).group(1)
        self.assertEqual(sorted(resume.LEASE_HOLDING), sorted(re.findall(r"Status\.([A-Z_]+)", declared)))


class EnableMethodTests(unittest.TestCase):
    def test_the_narrow_control_function_wins_over_ownership(self):
        subscription = {"can_call_control": True, "can_alter": True}
        self.assertEqual(resume.enable_method(subscription), "control_function")

    def test_ownership_is_the_fallback_and_nothing_else_is_tried(self):
        self.assertEqual(resume.enable_method({"can_call_control": False, "can_alter": True}), "alter_subscription")
        self.assertIsNone(resume.enable_method({"can_call_control": False, "can_alter": False}))

    def test_only_the_forward_subscription_can_be_named(self):
        for method in ("control_function", "alter_subscription"):
            statement = resume.enable_statement(method)
            self.assertIn("t8_source_to_target", statement)
            self.assertNotIn("t8_target_to_source", statement)
            self.assertNotIn("DISABLE", statement)

    def test_every_read_statement_only_selects(self):
        for name, statement in resume.STATEMENTS.items():
            with self.subTest(name=name):
                self.assertRegex(statement.strip(), r"^SELECT\b")
                # A privilege name inside a quoted argument is not a statement.
                code = re.sub(r"'[^']*'", "''", statement).upper()
                self.assertNotRegex(code, r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|GRANT)\b")

    def test_no_read_statement_can_select_the_connection_string(self):
        for statement in resume.STATEMENTS.values():
            self.assertNotIn("subconninfo", statement)

    def test_the_probe_writes_only_its_own_row_in_the_barrier_table(self):
        self.assertRegex(resume.PROBE_INSERT, r"^INSERT INTO cutover\.barrier \(barrier_id, epoch\)")
        self.assertRegex(resume.PROBE_DELETE, r"^DELETE FROM cutover\.barrier WHERE barrier_id = %s$")

    def test_probe_permission_needs_both_privileges_in_separate_calls(self):
        # has_table_privilege is true when any listed privilege is held; cutover_control has only INSERT.
        statement = resume.STATEMENTS["probe_allowed"]
        self.assertIn("'INSERT')", statement)
        self.assertIn("'DELETE')", statement)
        self.assertNotIn("INSERT,DELETE", statement)

    def test_the_catalog_reads_do_not_raise_on_a_schema_the_role_cannot_use(self):
        for statement in resume.STATEMENTS.values():
            self.assertNotIn("to_regclass", statement)
            self.assertNotIn("to_regprocedure", statement)


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.baseline = resume.baseline_of(healthy())

    def checks(self, state, rows_arrived=True):
        return resume.assess(self.baseline, state, rows_arrived=rows_arrived)

    def test_everything_true_after_a_real_catch_up(self):
        self.assertTrue(all(self.checks(caught_up()).values()))

    def test_a_worker_that_has_not_reached_the_publisher_is_not_connected(self):
        state = caught_up()
        state["source"]["slot"]["active"] = False
        self.assertFalse(self.checks(state)["publisher_connected"])

    def test_the_applied_position_must_also_pass_the_watermark(self):
        state = caught_up()
        state["target"]["subscription"]["latest_end_lsn"] = "0/5FFFFFF"
        self.assertFalse(self.checks(state)["backlog_cleared"])
        state["target"]["subscription"]["latest_end_lsn"] = None
        self.assertFalse(self.checks(state)["backlog_cleared"])

    def test_no_worker_means_not_confirmed(self):
        state = caught_up()
        state["target"]["subscription"]["worker_pid"] = None
        self.assertFalse(self.checks(state)["worker_running"])

    def test_position_that_did_not_move_is_not_progress(self):
        state = caught_up()
        state["source"]["slot"]["confirmed_flush_lsn"] = "0/1000000"
        checks = self.checks(state)
        self.assertFalse(checks["lsn_advanced"])
        self.assertFalse(checks["backlog_cleared"])

    def test_movement_short_of_the_watermark_is_progress_but_not_caught_up(self):
        state = caught_up()
        state["source"]["slot"]["confirmed_flush_lsn"] = "0/5FFFFFF"
        checks = self.checks(state)
        self.assertTrue(checks["lsn_advanced"])
        self.assertFalse(checks["backlog_cleared"])

    def test_no_backlog_needs_the_worker_to_report_the_watermark(self):
        idle = healthy()
        idle["source"]["slot"]["confirmed_flush_lsn"] = "0/6000000"
        baseline = resume.baseline_of(idle)
        state = caught_up()
        state["source"]["slot"]["confirmed_flush_lsn"] = "0/6000000"
        state["target"]["subscription"]["received_lsn"] = None
        self.assertFalse(resume.assess(baseline, state, rows_arrived=True)["lsn_advanced"])
        state["target"]["subscription"]["received_lsn"] = "0/6000000"
        self.assertTrue(resume.assess(baseline, state, rows_arrived=True)["lsn_advanced"])

    def test_any_new_error_or_conflict_fails(self):
        for field in ("apply_error_count", "sync_error_count", "conflicts"):
            with self.subTest(field=field):
                state = caught_up()
                state["target"]["subscription"][field] += 1
                self.assertFalse(self.checks(state)["errors_not_increased"])

    def test_tables_still_copying_are_not_caught_up(self):
        state = caught_up()
        state["target"]["subscription"]["tables_not_ready"] = 1
        self.assertFalse(self.checks(state)["tables_ready"])

    def test_the_single_writer_rule_is_rechecked_at_the_end(self):
        for path, value in ((["target", "app_can_login"], True), (["target", "app_sessions"], 1),
                            (["source", "app_can_login"], False), (["source", "reverse", "enabled"], True)):
            with self.subTest(path=path):
                state = caught_up()
                node = state
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                self.assertFalse(self.checks(state)["single_writer_kept"])

    def test_a_vanished_subscription_or_slot_is_false_rather_than_a_crash(self):
        state = caught_up()
        state["target"]["subscription"] = None
        self.assertFalse(self.checks(state)["subscription_enabled"])
        state = caught_up()
        state["source"]["slot"] = None
        self.assertFalse(self.checks(state)["slot_valid"])

    def test_rows_are_compared_against_the_watermark_snapshot(self):
        rows = resume.rows_arrived
        watermark = healthy()["source_rows"]
        self.assertTrue(rows(watermark, {"messages": 10, "messages_max_id": 10, "attachments": 2}))
        self.assertTrue(rows(watermark, {"messages": 12, "messages_max_id": 12, "attachments": 3}))
        self.assertFalse(rows(watermark, {"messages": 9, "messages_max_id": 10, "attachments": 2}))
        self.assertFalse(rows(watermark, {"messages": 10, "messages_max_id": 9, "attachments": 2}))
        self.assertFalse(rows(watermark, {"messages": 10, "messages_max_id": 10, "attachments": 1}))
        self.assertFalse(rows(watermark, {"messages": 0, "messages_max_id": None, "attachments": 2}))

    def test_an_empty_source_needs_no_rows(self):
        empty = {"messages": 0, "messages_max_id": None, "attachments": 0, "wal_lsn": "0/6000000"}
        self.assertTrue(resume.rows_arrived(empty, {"messages": 0, "messages_max_id": None, "attachments": 0}))


def starting():
    """Just after ENABLE: the flag is on and the launcher has not started a worker yet."""
    state = healthy()
    state["target"]["subscription"]["enabled"] = True
    return state


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PostCheckTests(unittest.TestCase):
    def run_loop(self, reads, *, timeout=10, settle=2, poll=1):
        clock = Clock()
        sequence = iter(reads)
        last = {}

        def status():
            value = next(sequence, last.get("value"))
            if isinstance(value, Exception):
                raise value
            last["value"] = value
            return value

        return resume.post_check(resume.baseline_of(healthy()), status, lambda: caught_up()["target_rows"],
                                 timeout=timeout, settle=settle, poll=poll, clock=clock, sleep=clock.sleep)

    def test_passes_after_staying_good_for_the_settle_period(self):
        result = self.run_loop([starting(), caught_up()])
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "confirmed")
        self.assertGreaterEqual(result["seconds"], 2)
        self.assertEqual(result["backlog_cleared_seconds"], 1)
        self.assertEqual(result["positions"], {"watermark": "0/6000000", "confirmed_flush": "0/6000100", "latest_end": "0/6000100"})

    def test_stops_at_once_when_the_subscription_turns_itself_off_again(self):
        off = caught_up()
        off["target"]["subscription"]["enabled"] = False
        result = self.run_loop([off], timeout=600)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "failed_check")
        self.assertLess(result["seconds"], 5)

    def test_stops_at_once_on_a_new_error(self):
        errored = caught_up()
        errored["target"]["subscription"]["apply_error_count"] = 2
        result = self.run_loop([errored], timeout=600)
        self.assertEqual((result["passed"], result["reason"]), (False, "failed_check"))

    def test_stops_at_once_when_the_slot_is_invalidated_during_catch_up(self):
        lost = caught_up()
        lost["source"]["slot"]["invalidation_reason"] = "wal_removed"
        result = self.run_loop([lost], timeout=600)
        self.assertEqual((result["passed"], result["reason"]), (False, "failed_check"))

    def test_no_worker_until_the_deadline_is_a_failure(self):
        waiting = caught_up()
        waiting["target"]["subscription"]["worker_pid"] = None
        result = self.run_loop([waiting], timeout=5)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertFalse(result["checks"]["worker_running"])

    def test_reads_that_keep_failing_are_a_failure_not_a_pass(self):
        result = self.run_loop([OSError(f"host=target.invalid password={SECRET}")] * 20, timeout=5)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertEqual(result["read_errors"], {"OSError": 6})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_a_brief_read_failure_does_not_end_the_check(self):
        result = self.run_loop([OSError("blip"), caught_up()])
        self.assertTrue(result["passed"])

    def test_the_settle_period_restarts_when_the_worker_goes_away(self):
        gone = caught_up()
        gone["target"]["subscription"]["worker_pid"] = None
        result = self.run_loop([caught_up(), gone, caught_up()], settle=2)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["seconds"], 4)

    def test_it_reports_every_worker_it_saw(self):
        second = caught_up()
        second["target"]["subscription"]["worker_pid"] = 4343
        result = self.run_loop([caught_up(), second], settle=1)
        self.assertEqual(result["worker_pids_seen"], 2)

    def test_rows_that_never_arrive_fail_at_the_deadline(self):
        clock = Clock()
        short = {"messages": 1, "messages_max_id": 1, "attachments": 2}
        result = resume.post_check(resume.baseline_of(healthy()), caught_up, lambda: short,
                                   timeout=4, settle=1, poll=1, clock=clock, sleep=clock.sleep)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertFalse(result["checks"]["rows_arrived"])


class BarrierProbeTests(unittest.TestCase):
    def probe(self, counts, *, write=None, timeout=3):
        clock = Clock()
        sequence = iter(counts)
        writes = []

        def read():
            value = next(sequence, counts[-1])
            if isinstance(value, Exception):
                raise value
            return value

        def record(statement, params):
            writes.append((statement, params))
            if write:
                write(statement)

        report = resume.barrier_probe(record, read, "id-1", timeout=timeout, poll=1, clock=clock, sleep=clock.sleep)
        return report, writes

    def test_row_arrives_and_its_delete_follows(self):
        report, writes = self.probe([0, 1, 1, 0])
        self.assertTrue(report["passed"])
        self.assertEqual(report["arrived_ms"], 1000)
        self.assertEqual(report["delete_arrived_ms"], 1000)
        self.assertEqual([statement.split()[0] for statement, _ in writes], ["INSERT", "DELETE"])
        self.assertEqual(writes[0][1], ("id-1", resume.PROBE_EPOCH))
        self.assertEqual(writes[1][1], ("id-1",))

    def test_a_row_that_never_arrives_fails_and_is_still_deleted_from_the_source(self):
        report, writes = self.probe([0])
        self.assertFalse(report["passed"])
        self.assertIsNone(report["arrived_ms"])
        self.assertTrue(report["deleted_on_source"])
        self.assertEqual(len(writes), 2)

    def test_a_delete_that_never_arrives_fails(self):
        report, _ = self.probe([1])
        self.assertFalse(report["passed"])
        self.assertEqual(report["arrived_ms"], 0)
        self.assertIsNone(report["delete_arrived_ms"])

    def test_a_refused_insert_fails_without_its_message(self):
        class Denied(Exception):
            sqlstate = "42501"

        def refuse(_):
            raise Denied(f"password={SECRET}")

        report, _ = self.probe([0], write=refuse)
        self.assertFalse(report["passed"])
        self.assertFalse(report["inserted"])
        self.assertEqual((report["error_category"], report["sqlstate"]), ("Denied", "42501"))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_a_failed_delete_reports_the_row_it_left(self):
        def refuse_delete(statement):
            if statement.startswith("DELETE"):
                raise OSError("no route")

        report, _ = self.probe([1], write=refuse_delete)
        self.assertFalse(report["passed"])
        self.assertTrue(report["inserted"])
        self.assertFalse(report["deleted_on_source"])
        self.assertEqual(report["barrier_id"], "id-1")

    def test_a_read_failure_counts_as_not_yet(self):
        report, _ = self.probe([OSError("blip"), 1, 0])
        self.assertTrue(report["passed"])


def fake_read(state):
    """Reads a prepared state; the first argument is which database the DSN named."""
    def read(dsn, name, params=None):
        if "source.invalid" in dsn:
            return state["source_rows"] if name == "source_rows" else state["source"]
        if "target.invalid" in dsn:
            return state["target_rows"] if name == "target_rows" else state["target"]
        if "probe.invalid" in dsn:
            return state["probe_allowed"]
        return state["executor"]["holders"]
    return read


class GatherTests(unittest.TestCase):
    def test_the_target_is_read_before_the_source(self):
        # Rows keep arriving during the reads. Reading the source first would show a healthy target ahead of it.
        order = []
        state = healthy()

        def read(dsn, name, params=None):
            order.append(name)
            return fake_read(state)(dsn, name, params)

        with patch.object(resume, "read_one", side_effect=read):
            resume.gather(ENVIRON["T8_RESUME_SOURCE_DSN"], ENVIRON["T8_RESUME_TARGET_DSN"], None, False)
        self.assertLess(order.index("target_rows"), order.index("source_rows"))


class RunTests(unittest.TestCase):
    def run_tool(self, state, *arguments, environ=ENVIRON):
        with (patch.object(resume, "read_one", side_effect=fake_read(state)),
              patch.object(resume, "enable") as enable):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(list(arguments), environ)
        return code, json.loads(buffer.getvalue()), enable

    def test_dry_run_is_the_default_and_never_enables(self):
        code, report, enable = self.run_tool(healthy())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dry-run")
        self.assertIn("t8_source_to_target", report["would_run"])
        enable.assert_not_called()

    def test_refused_dry_run_exits_nonzero_and_names_the_reason(self):
        code, report, enable = self.run_tool(changed(["source", "reverse", "enabled"], True))
        self.assertEqual(code, 1)
        self.assertIn("reverse_active", report["refusals"])
        self.assertNotIn("would_run", report)
        enable.assert_not_called()

    def test_refused_apply_never_enables(self):
        code, report, enable = self.run_tool(changed(["target", "app_can_login"], True), "--apply", *PINS)
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "preflight")
        enable.assert_not_called()

    def test_apply_enables_once_then_verifies(self):
        reads = iter([healthy()])

        def status(*_):
            return next(reads, caught_up())

        with (patch.object(resume, "gather", side_effect=lambda *_: next(reads, caught_up())),
              patch.object(resume, "enable") as enable, patch.object(resume, "read_status", side_effect=status),
              patch.object(resume, "read_one", return_value=caught_up()["target_rows"])):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", "--no-barrier-probe", *PINS, "--settle", "0", "--poll", "0"], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, report)
        enable.assert_called_once()
        self.assertEqual(report["enabled_by"], "alter_subscription")
        self.assertTrue(report["post_check"]["passed"])

    def test_apply_on_a_running_subscription_never_enables_again(self):
        code, report, enable = self.run_tool(caught_up(), "--apply", "--no-barrier-probe", *PINS, "--settle", "0", "--poll", "0")
        enable.assert_not_called()
        self.assertIsNone(report["enabled_by"])
        self.assertEqual(code, 0, report)
        self.assertTrue(report["post_check"]["passed"])

    def test_the_default_on_a_running_subscription_runs_the_read_only_post_check(self):
        with patch.object(resume, "write_probe") as write:
            code, report, enable = self.run_tool(caught_up(), "--settle", "0", "--poll", "0")
        self.assertEqual(code, 0, report)
        self.assertIsNone(report["would_run"])
        self.assertTrue(report["post_check"]["passed"])
        self.assertNotIn("barrier_probe", report)
        enable.assert_not_called()
        write.assert_not_called()

    def test_the_default_needs_no_privilege_on_a_running_subscription(self):
        state = caught_up()
        state["target"]["subscription"].update(can_alter=False, can_call_control=False)
        code, report, _ = self.run_tool(state, "--settle", "0", "--poll", "0")
        self.assertEqual(code, 0, report)

    def test_the_default_on_a_stopped_subscription_ends_at_the_preflight(self):
        code, report, _ = self.run_tool(healthy())
        self.assertEqual(code, 0)
        self.assertNotIn("post_check", report)

    def test_apply_needs_both_identifiers(self):
        code, report, enable = self.run_tool(healthy(), "--apply", "--no-barrier-probe")
        self.assertEqual(code, 1)
        self.assertIn("source_identity_not_pinned", report["refusals"])
        self.assertIn("target_identity_not_pinned", report["refusals"])
        enable.assert_not_called()

    def test_a_wrong_identifier_is_refused_even_in_a_dry_run(self):
        code, report, _ = self.run_tool(healthy(), "--target-system-id", "1")
        self.assertEqual(code, 1)
        self.assertIn("target_identity_mismatch", report["refusals"])

    def test_the_dry_run_prints_the_identifiers_to_pin(self):
        _, report, _ = self.run_tool(healthy())
        self.assertEqual(report["preflight"]["source"]["system_identifier"], SOURCE_ID)
        self.assertEqual(report["preflight"]["target"]["system_identifier"], TARGET_ID)

    def test_apply_without_a_probe_dsn_is_refused_before_any_connection(self):
        environ = {key: value for key, value in ENVIRON.items() if key != "T8_RESUME_SOURCE_PROBE_DSN"}
        with patch.object(resume, "read_one") as read:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], environ)
        self.assertEqual(code, 1)
        read.assert_not_called()
        self.assertEqual(json.loads(buffer.getvalue())["refusals"], ["missing_T8_RESUME_SOURCE_PROBE_DSN"])

    def test_a_dry_run_never_reads_or_uses_the_probe_dsn(self):
        seen = []
        state = healthy()

        def read(dsn, name, params=None):
            seen.append(dsn)
            return fake_read(state)(dsn, name, params)

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                resume.main([], ENVIRON)
        self.assertFalse([dsn for dsn in seen if "probe.invalid" in dsn])

    def test_a_barrier_outside_the_publication_is_reported_by_a_dry_run(self):
        code, report, _ = self.run_tool(changed(["source", "barrier_published"], False))
        self.assertEqual(code, 0)
        self.assertEqual(report["barrier_gaps"], ["barrier_not_published"])

    def test_apply_sends_the_probe_after_the_checks_and_reads_them_again(self):
        order = []

        def probe(*_args, **_kwargs):
            order.append("probe")
            return {"passed": True}

        def check(*_args, **_kwargs):
            order.append("check")
            return {"passed": True}

        with (patch.object(resume, "barrier_probe", side_effect=probe), patch.object(resume, "post_check", side_effect=check),
              patch.object(resume, "read_one", side_effect=fake_read(caught_up()))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], ENVIRON)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["check", "probe", "check"])

    def test_a_failed_post_check_never_writes_the_probe_row(self):
        with (patch.object(resume, "barrier_probe") as probe, patch.object(resume, "post_check", return_value={"passed": False}),
              patch.object(resume, "read_one", side_effect=fake_read(caught_up()))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], ENVIRON)
        self.assertEqual(code, 1)
        probe.assert_not_called()

    def test_a_failed_probe_fails_the_run(self):
        with (patch.object(resume, "barrier_probe", return_value={"passed": False}),
              patch.object(resume, "post_check", return_value={"passed": True}),
              patch.object(resume, "read_one", side_effect=fake_read(caught_up()))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], ENVIRON)
        self.assertEqual(code, 1)

    def test_new_errors_after_the_probe_fail_the_run(self):
        checks = iter([{"passed": True}, {"passed": False}])
        with (patch.object(resume, "barrier_probe", return_value={"passed": True}),
              patch.object(resume, "post_check", side_effect=lambda *_a, **_k: next(checks)),
              patch.object(resume, "read_one", side_effect=fake_read(caught_up()))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], ENVIRON)
        self.assertEqual(code, 1)

    def test_a_failed_enable_is_reported_without_its_message(self):
        class Denied(Exception):
            sqlstate = "42501"

        with (patch.object(resume, "gather", return_value=healthy()),
              patch.object(resume, "enable", side_effect=Denied(f"host=target.invalid password={SECRET}"))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", "--no-barrier-probe", *PINS], ENVIRON)
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, buffer.getvalue())
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["stage"], "enable")
        self.assertEqual((report["error_category"], report["sqlstate"]), ("Denied", "42501"))

    def test_a_missing_dsn_is_refused_before_any_connection(self):
        with patch.object(resume, "read_one") as read:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([], {})
        self.assertEqual(code, 1)
        read.assert_not_called()
        self.assertEqual(json.loads(buffer.getvalue())["refusals"], ["missing_T8_RESUME_SOURCE_DSN", "missing_T8_RESUME_TARGET_DSN"])

    def test_an_unreadable_side_becomes_a_refusal_with_only_a_class_name(self):
        def read(dsn, *_args):
            raise OSError(f"could not connect: {dsn}")

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([], ENVIRON)
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, buffer.getvalue())
        self.assertNotIn(".invalid", buffer.getvalue())
        report = json.loads(buffer.getvalue())
        self.assertIn("source_unconfirmed", report["refusals"])
        self.assertIn("target_unconfirmed", report["refusals"])
        self.assertEqual(report["errors"]["source"], "OSError")

    def test_an_unexpected_exception_prints_only_its_class(self):
        with patch.object(resume, "gather", side_effect=RuntimeError(f"password={SECRET}")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([], ENVIRON)
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["error_category"], "RuntimeError")

    def test_without_executor_access_the_state_is_unconfirmed(self):
        environ = {key: value for key, value in ENVIRON.items() if key != "T8_RESUME_EXECUTOR_DSN"}
        code, report, _ = self.run_tool(healthy(), environ=environ)
        self.assertEqual(code, 1)
        self.assertIn("executor_state_unconfirmed", report["refusals"])

    def test_an_operator_can_declare_no_executor_and_the_receipt_says_so(self):
        environ = {key: value for key, value in ENVIRON.items() if key != "T8_RESUME_EXECUTOR_DSN"}
        code, report, _ = self.run_tool(healthy(), "--executor-not-deployed", environ=environ)
        self.assertEqual(code, 0)
        self.assertEqual(report["preflight"]["executor"], {"state": "declared_not_deployed"})

    def test_the_declaration_cannot_hide_a_running_executor(self):
        state = healthy()
        state["executor"] = {"state": "holding", "holders": 1}
        code, report, _ = self.run_tool(state, "--executor-not-deployed")
        self.assertEqual(code, 1)
        self.assertIn("executor_running", report["refusals"])

    def test_control_sessions_still_block_a_declared_absent_executor(self):
        environ = {key: value for key, value in ENVIRON.items() if key != "T8_RESUME_EXECUTOR_DSN"}
        state = copy.deepcopy(healthy())
        state["source"]["control_sessions"] = 2
        code, report, _ = self.run_tool(state, "--executor-not-deployed", environ=environ)
        self.assertEqual(code, 1)
        self.assertIn("control_session_present", report["refusals"])

    def test_the_report_never_carries_a_dsn(self):
        _, report, _ = self.run_tool(healthy())
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(".invalid", json.dumps(report))


class TlsTests(unittest.TestCase):
    def setUp(self):
        # Default test discovery has no driver installed; the decision must come before it is used anyway.
        self.driver = types.SimpleNamespace(
            connect=Mock(), conninfo=types.SimpleNamespace(
                conninfo_to_dict=lambda dsn: dict(pair.split("=", 1) for pair in dsn.split())))

    def test_plaintext_to_a_remote_host_is_refused_before_connecting(self):
        with patch.dict(sys.modules, {"psycopg": self.driver}), self.assertRaises(resume.AttachmentCopyFailed):
            resume.connect("host=db.invalid sslmode=require password=x", {})
        self.driver.connect.assert_not_called()

    def test_the_loopback_opt_in_covers_only_literal_loopback(self):
        with patch.dict(sys.modules, {"psycopg": self.driver}), self.assertRaises(resume.AttachmentCopyFailed):
            resume.connect("host=db.invalid password=x", {resume.LOOPBACK_ENV: "1"})
        self.driver.connect.assert_not_called()

    def test_the_refusal_reaches_the_report_as_its_fixed_reason(self):
        with patch.dict(sys.modules, {"psycopg": self.driver}):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([], ENVIRON)
        self.assertEqual(code, 1)
        self.driver.connect.assert_not_called()
        self.assertNotIn(SECRET, buffer.getvalue())
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["errors"]["source"], "database connection must use verify-full TLS")
        self.assertIn("source_unconfirmed", report["refusals"])

    def test_the_read_session_rejects_a_server_that_does_not_confirm_read_only(self):
        class Result:
            def fetchone(self):
                return ("off",)

        class Connection:
            autocommit = True
            read_only = None
            closed = False

            def execute(self, *_):
                return Result()

            def rollback(self):
                pass

            def close(self):
                self.closed = True

        connection = Connection()
        with patch.object(resume, "connect", return_value=connection), self.assertRaises(resume.NotReadOnly):
            resume.read_one("dsn", "source_status")
        self.assertTrue(connection.closed)


class LogEventTests(unittest.TestCase):
    def test_only_known_events_are_kept_and_never_the_raw_text(self):
        lines = ["2026-11-04 05:00:00.000 UTC 08006 ERROR:  terminating logical replication worker due to timeout",
                 '2026-11-04 05:00:00.018 UTC 00000 LOG:  subscription "t8_source_to_target" has been disabled because of an error',
                 '2026-11-04 05:00:01.000 UTC 08006 ERROR:  could not connect to the publisher: connection to server at "198.51.100.7"',
                 "2026-11-04 05:00:02.000 UTC 00000 LOG:  checkpoint starting: time",
                 "not a log line at all"]
        events = metrics.parse_log_events(lines)
        self.assertEqual([event["event"] for event in events],
                         ["receiver_timeout", "subscription_disabled", "publisher_connect_failed"])
        self.assertEqual(events[0]["sqlstate"], "08006")
        self.assertEqual(events[1]["after_first_ms"], 18)
        self.assertNotIn("198.51.100.7", json.dumps(events))

    def test_each_event_is_timed_from_the_first_kept_event(self):
        lines = ["2026-11-04 05:00:10.500 UTC 08006 ERROR:  terminating walsender process due to replication timeout"]
        self.assertEqual(metrics.parse_log_events(lines)[0]["after_first_ms"], 0)

    def test_nothing_to_report_is_an_empty_list(self):
        self.assertEqual(metrics.parse_log_events([]), [])


class TimeoutSummaryTests(unittest.TestCase):
    def trial(self, outcome, *, receiver=5, sender=5, disable_on_error=True, blip=8, loss=100, recovered=None):
        return {"receiver_s": receiver, "sender_s": sender, "disable_on_error": disable_on_error, "blip_s": blip,
                "loss_pct": loss, "outcome": outcome, "disabled_after_s": 5.1 if outcome == "disabled" else None,
                "rows_flowing_after_link_up_s": recovered}

    def test_groups_by_setting_and_counts_outcomes(self):
        rows = metrics.summarize_timeout_trials([
            self.trial("disabled"), self.trial("disabled"), self.trial("recovered", recovered=2.0),
            self.trial("recovered", receiver=60, sender=60, recovered=1.0),
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["runs"], 3)
        self.assertEqual((rows[0]["disabled"], rows[0]["recovered"], rows[0]["neither"]), (2, 1, 0))
        self.assertEqual(rows[0]["rows_flowing_after_link_up_s_max"], 2.0)
        self.assertEqual(rows[1]["disabled"], 0)

    def test_an_outcome_that_is_neither_is_not_hidden(self):
        rows = metrics.summarize_timeout_trials([self.trial("no_recovery_seen")])
        self.assertEqual(rows[0]["neither"], 1)
        self.assertIsNone(rows[0]["rows_flowing_after_link_up_s_max"])


class LabRuleTests(unittest.TestCase):
    def test_link_down_only_changes_the_named_interface(self):
        self.assertEqual(lab.link_down_script("eth1", 100), "tc qdisc replace dev eth1 root netem loss 100%")
        with self.assertRaises(ValueError):
            lab.link_down_script("eth1; rm -rf /", 100)

    def test_link_up_restores_the_shaping_or_removes_the_rule(self):
        self.assertEqual(lab.link_up_script("eth1", None), "tc qdisc del dev eth1 root")
        self.assertEqual(lab.link_up_script("eth1", {"mbit": 20, "rtt_ms": 30}), lab.shaping_script("eth1", {"mbit": 20, "rtt_ms": 30}))

    def test_lab_parameters_add_to_the_defaults_without_changing_them(self):
        before = dict(lab.DB_PARAMETERS)
        chosen = lab.Lab(None, parameters={"wal_receiver_timeout": "60s"}).parameters
        self.assertEqual(chosen["wal_receiver_timeout"], "60s")
        self.assertEqual(chosen["wal_level"], "logical")
        self.assertEqual(lab.DB_PARAMETERS, before)
        self.assertEqual(lab.Lab(None).parameters, before)


if __name__ == "__main__":
    unittest.main()
