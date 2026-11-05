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
FAST = ("--settle", "0", "--poll", "0", "--probe-poll", "0")
TABLES = {"messages": {"rows": 10, "sha256": "aa"}, "attachments": {"rows": 2, "sha256": "bb"}}
EVERY_TABLE_READY = {"public.messages": "r", "public.attachments": "r", "cutover.barrier": "r", "cutover.probe": "r"}


def without(*names):
    return {key: value for key, value in ENVIRON.items() if key not in names}


def healthy():
    """The state reported by the actual incident: the forward subscription off, everything else as designed."""
    return {
        "source": {
            "source_observed_at": "2026-11-04T07:26:45+00:00", "system_identifier": SOURCE_ID, "database": "chat",
            "slot_database": "chat", "in_recovery": False,
            "barrier_published": True, "barrier_readable": True, "source_app_login": True, "control_sessions": 0,
            "source_wal_flush_lsn": "0/6000000", "source_wal_sender_timeout_ms": 5000, "max_slot_wal_keep_size": "1GB",
            "slot_exists": True, "slot_type": "logical", "source_slot_active": False, "source_slot_wal_status": "reserved",
            "source_slot_invalidated": False, "invalidation_reason": None, "safe_wal_size": 900,
            "source_slot_confirmed_flush_lsn": "0/1000000", "source_sender_state": None, "source_sender_tls": None,
            "reverse_enabled": False, "reverse_workers": 0,
        },
        "target": {
            "target_observed_at": "2026-11-04T07:26:46+00:00", "system_identifier": TARGET_ID, "database": "chat", "in_recovery": False,
            "target_app_login": False, "app_sessions": 0, "control_sessions": 0,
            "target_wal_receiver_timeout_ms": 5000, "reverse_slot_active": False, "subscription_exists": True,
            "subscription_oid": 16400, "forward_enabled": False, "disable_on_error": True,
            "table_sync_states": dict(EVERY_TABLE_READY), "apply_workers": 0, "worker_pid": None,
            "received_lsn": None, "latest_end_lsn": None, "origin_readable": True, "target_origin_remote_lsn": None,
            "target_apply_errors": 1, "target_sync_errors": 0, "target_conflicts": 0,
            "barrier_readable": True, "can_alter": True, "can_call_control": False,
        },
        "source_rows": {"messages": 10, "messages_max_id": 10, "attachments": 2, "attachments_max_created_at": "2026-11-04 00:00:00+00"},
        "target_rows": {"messages": 8, "messages_max_id": 8, "attachments": 2},
        "executor": {"state": "idle", "holding": 0, "executions": 0, "leases": 0, "resource_fences": 0},
        "probe": {"system_identifier": SOURCE_ID, "database": "chat", "in_recovery": False, "allowed": True},
    }


def changed(path, value):
    state = healthy()
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return state


def starting():
    """Just after ENABLE: the flag is on and the launcher has not started a worker yet."""
    return changed(["target", "forward_enabled"], True)


def caught_up():
    """What a read shows once the target has confirmed everything up to the fixed position."""
    state = healthy()
    state["target"].update(forward_enabled=True, apply_workers=1, worker_pid=4242, received_lsn="0/6000100",
                           latest_end_lsn="0/6000100", target_origin_remote_lsn="0/6000200")
    state["source"].update(source_slot_active=True, source_slot_confirmed_flush_lsn="0/6000100",
                           source_sender_state="streaming", source_sender_tls=True)
    state["target_rows"] = {"messages": 10, "messages_max_id": 10, "attachments": 2}
    return state


class RefusalTests(unittest.TestCase):
    def test_the_reported_incident_state_passes(self):
        self.assertEqual(resume.refusals(healthy()), [])

    def test_each_unsafe_state_is_refused_for_its_own_reason(self):
        cases = {
            "slot_missing": changed(["source", "slot_exists"], False),
            "slot_invalidated": changed(["source", "source_slot_invalidated"], True),
            "slot_at_risk": changed(["source", "source_slot_wal_status"], "unreserved"),
            "slot_not_logical": changed(["source", "slot_type"], "physical"),
            "slot_in_use": changed(["source", "source_slot_active"], True),
            "subscription_missing": changed(["target", "subscription_exists"], False),
            "reverse_active": changed(["source", "reverse_enabled"], True),
            "reverse_slot_in_use": changed(["target", "reverse_slot_active"], True),
            "target_app_can_login": changed(["target", "target_app_login"], True),
            "target_app_sessions": changed(["target", "app_sessions"], 1),
            "source_app_cannot_login": changed(["source", "source_app_login"], False),
            "source_is_not_primary": changed(["source", "in_recovery"], True),
            "target_is_not_primary": changed(["target", "in_recovery"], True),
            "executor_running": changed(["executor"], {"state": "holding", "holding": 1}),
            "executor_state_unconfirmed": changed(["executor"], {"state": "unconfirmed"}),
            "control_session_present": changed(["target", "control_sessions"], 1),
            "target_ahead_of_source": changed(["target_rows", "messages_max_id"], 11),
            "source_and_target_are_one_cluster": changed(["target", "system_identifier"], SOURCE_ID),
            "source_wrong_database": changed(["source", "slot_database"], "other"),
        }
        for reason, state in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, resume.refusals(state))

    def test_a_running_reverse_worker_counts_even_if_the_flag_says_off(self):
        self.assertIn("reverse_active", resume.refusals(changed(["source", "reverse_workers"], 1)))

    def test_lost_wal_status_is_invalidated_without_a_reason_column(self):
        self.assertIn("slot_at_risk", resume.refusals(changed(["source", "source_slot_wal_status"], "unreserved")))
        lost = changed(["source", "source_slot_wal_status"], "lost")
        lost["source"]["source_slot_invalidated"] = True
        self.assertIn("slot_invalidated", resume.refusals(lost))
        self.assertNotIn("slot_at_risk", resume.refusals(lost))

    def test_a_role_that_cannot_be_read_is_not_a_safe_role(self):
        self.assertIn("target_app_can_login", resume.refusals(changed(["target", "target_app_login"], None)))
        self.assertIn("source_app_cannot_login", resume.refusals(changed(["source", "source_app_login"], None)))

    def test_the_slot_is_expected_to_be_held_once_the_subscription_runs(self):
        self.assertEqual(resume.refusals(caught_up()), [])

    def test_no_privilege_to_enable_is_refused_only_when_enabling_is_needed(self):
        self.assertIn("no_enable_privilege", resume.refusals(changed(["target", "can_alter"], False)))
        running = caught_up()
        running["target"]["can_alter"] = False
        self.assertEqual(resume.refusals(running), [])

    def test_a_barrier_outside_the_publication_only_stops_the_probe(self):
        not_subscribed = healthy()
        del not_subscribed["target"]["table_sync_states"]["cutover.barrier"]
        for state, reason in ((changed(["source", "barrier_published"], False), "barrier_not_published"),
                              (not_subscribed, "barrier_not_subscribed"),
                              (changed(["target", "barrier_readable"], False), "barrier_not_readable_on_target"),
                              (changed(["source", "barrier_readable"], False), "barrier_not_readable_on_source"),
                              (changed(["probe", "allowed"], False), "barrier_probe_not_permitted"),
                              (changed(["probe", "system_identifier"], TARGET_ID), "barrier_probe_wrong_database"),
                              (changed(["probe", "database"], "other"), "barrier_probe_wrong_database"),
                              (changed(["probe", "in_recovery"], True), "barrier_probe_not_primary"),
                              (changed(["probe"], None), "barrier_probe_unconfirmed")):
            with self.subTest(reason=reason):
                self.assertEqual(resume.refusals(state), [])
                self.assertIn(reason, resume.refusals(state, probe=True))

    def test_permission_on_another_database_is_not_enough(self):
        # verify-full proves which server answered, not that it is the source verified above.
        state = changed(["probe", "system_identifier"], TARGET_ID)
        self.assertTrue(state["probe"]["allowed"])
        self.assertEqual(resume.refusals(state, probe=True), ["barrier_probe_wrong_database"])

    def test_a_role_that_cannot_read_the_applied_position_can_still_probe(self):
        # The Executor's role cannot read it. A marker's round trip proves the same thing.
        self.assertEqual(resume.refusals(changed(["target", "origin_readable"], False), probe=True), [])

    def test_a_barrier_copy_still_running_is_not_subscribed(self):
        state = healthy()
        state["target"]["table_sync_states"]["cutover.barrier"] = "d"
        self.assertIn("barrier_not_subscribed", resume.refusals(state, probe=True))

    def test_lease_statuses_match_the_executor_contract(self):
        contract = (ROOT / "services/cutover-executor/src/bravo_cutover/contract.py").read_text()
        declared = re.search(r"LEASE_HOLDING = \(([^)]*)\)", contract).group(1)
        self.assertEqual(sorted(resume.LEASE_HOLDING), sorted(re.findall(r"Status\.([A-Z_]+)", declared)))


class EnableMethodTests(unittest.TestCase):
    def test_the_narrow_control_function_wins_over_ownership(self):
        self.assertEqual(resume.enable_method({"can_call_control": True, "can_alter": True}), "control_function")

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
        # The epoch keeps it off the Executor's rows, which always have one of 1 or more.
        self.assertRegex(resume.PROBE_DELETE, r"^DELETE FROM cutover\.barrier WHERE barrier_id = %s AND epoch = %s$")
        self.assertEqual(resume.PROBE_EPOCH, 0)

    def test_probe_permission_needs_each_privilege_in_its_own_call(self):
        # has_table_privilege is true when any listed privilege is held; cutover_control has INSERT and SELECT.
        statement = resume.STATEMENTS["probe_status"]
        for privilege in ("'INSERT')", "'DELETE')", "'SELECT')"):
            self.assertIn(privilege, statement)
        self.assertNotIn("INSERT,DELETE", statement)

    def test_the_catalog_reads_do_not_raise_on_a_schema_the_role_cannot_use(self):
        for statement in resume.STATEMENTS.values():
            self.assertNotIn("to_regclass", statement)
            self.assertNotIn("to_regprocedure", statement)

    def test_the_restricted_origin_view_stays_out_of_the_status_read(self):
        # Privileges are checked before execution, so a guard inside one statement cannot protect the read.
        for name in ("source_status", "target_status"):
            self.assertNotIn("FROM pg_replication_origin_status", resume.STATEMENTS[name])
        self.assertIn("FROM pg_replication_origin_status", resume.STATEMENTS["target_origin"])

    def test_hashes_do_not_depend_on_the_session_time_zone(self):
        self.assertIn("timezone=UTC", resume.READ_ONLY_OPTIONS)

    def test_hashing_never_holds_a_whole_table_in_one_value(self):
        # A row digest per row, then a digest of those: 200 MB of rows must not sit in one backend of a 1 GiB database.
        statement = resume.STATEMENTS["business_tables"]
        self.assertIn("string_agg(encode(sha256(convert_to(m::text", statement)
        self.assertIn("string_agg(encode(sha256(convert_to(a::text", statement)

    def test_the_command_does_not_mistake_its_own_closing_session_for_a_control_session(self):
        for name in ("source_status", "target_status"):
            self.assertIn("application_name <> 't8_resume'", resume.STATEMENTS[name])


class NamingTests(unittest.TestCase):
    """The first actual resume (PR #201) is the reference, so a later run must read the same way."""

    def test_a_stage_uses_the_names_of_the_first_resume(self):
        stage = resume.stage_view(caught_up()["source"], caught_up()["target"])
        for name in ("source_observed_at", "target_observed_at", "forward_enabled", "reverse_enabled", "apply_workers",
                     "source_slot_active", "source_slot_wal_status", "source_slot_invalidated", "source_sender_state",
                     "source_sender_tls", "source_slot_confirmed_flush_lsn", "target_origin_remote_lsn",
                     "target_app_login", "target_apply_errors", "target_sync_errors"):
            self.assertIn(name, stage)

    def test_the_position_gap_is_flush_minus_confirmed_flush_in_both_units(self):
        state = healthy()
        state["source"].update(source_wal_flush_lsn="0/1C000000", source_slot_confirmed_flush_lsn="0/16000000")
        view = resume.before_enable_view(state["source"])
        self.assertEqual(view["gap_bytes"], 100_663_296)
        self.assertEqual(view["gap_mib"], 96)
        self.assertEqual((view["slot_active"], view["wal_status"]), (False, "reserved"))
        self.assertEqual(view["source_wal_flush_lsn"], "0/1C000000")
        self.assertEqual(view["source_slot_confirmed_flush_lsn"], "0/16000000")

    def test_tables_report_rows_and_only_whether_the_hashes_match(self):
        before = {"messages": {"rows": 2, "sha256": "aa"}, "attachments": {"rows": 1, "sha256": "bb"}}
        view = resume.tables_view(before, before, before)
        self.assertEqual(view["messages"], {"source_rows": 2, "target_rows": 2, "sha256_equal": True, "source_sha256_unchanged": True})
        self.assertNotIn("aa", json.dumps(view))
        changed_source = {"messages": {"rows": 3, "sha256": "cc"}, "attachments": {"rows": 1, "sha256": "bb"}}
        view = resume.tables_view(before, changed_source, before)
        self.assertEqual(view["messages"], {"source_rows": 3, "target_rows": 2, "sha256_equal": False, "source_sha256_unchanged": False})

    def test_timeouts_are_reported_in_milliseconds(self):
        for statement, name in (("source_status", "wal_sender_timeout"), ("target_status", "wal_receiver_timeout")):
            self.assertIn(f"name = '{name}'", resume.STATEMENTS[statement])
        self.assertIn("source_wal_sender_timeout_ms", resume.STATEMENTS["source_status"])
        self.assertIn("target_wal_receiver_timeout_ms", resume.STATEMENTS["target_status"])


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.baseline = resume.baseline_of(healthy())

    def checks(self, state, rows_arrived=True, applied_floor=None):
        return resume.assess(self.baseline, state, rows_arrived=rows_arrived, applied_floor=applied_floor)

    def test_everything_true_after_a_real_catch_up(self):
        self.assertTrue(all(self.checks(caught_up()).values()))

    def test_a_worker_that_has_not_reached_the_publisher_is_not_connected(self):
        state = caught_up()
        state["source"]["source_slot_active"] = False
        self.assertFalse(self.checks(state)["source_slot_active"])

    def test_the_fixed_position_needs_every_view_of_it(self):
        for side, field in (("target", "latest_end_lsn"), ("source", "source_slot_confirmed_flush_lsn")):
            for value in ("0/5FFFFFF", None):
                with self.subTest(field=field, value=value):
                    state = caught_up()
                    state[side][field] = value
                    self.assertFalse(self.checks(state)["confirmed_fixed"])

    def test_receiving_the_position_is_reported_separately(self):
        state = caught_up()
        state["target"]["received_lsn"] = "0/5FFFFFF"
        self.assertFalse(self.checks(state)["received_fixed"])
        state["target"]["received_lsn"] = "0/6000000"
        self.assertTrue(self.checks(state)["received_fixed"])

    def test_no_worker_means_not_confirmed(self):
        state = caught_up()
        state["target"]["apply_workers"] = 0
        self.assertFalse(self.checks(state)["apply_worker_running"])

    def test_position_that_did_not_move_is_not_progress(self):
        state = caught_up()
        state["source"]["source_slot_confirmed_flush_lsn"] = "0/1000000"
        checks = self.checks(state)
        self.assertFalse(checks["confirmed_flush_advanced"])
        self.assertFalse(checks["confirmed_fixed"])

    def test_movement_short_of_the_fixed_position_is_progress_but_not_caught_up(self):
        state = caught_up()
        state["source"]["source_slot_confirmed_flush_lsn"] = "0/5FFFFFF"
        checks = self.checks(state)
        self.assertTrue(checks["confirmed_flush_advanced"])
        self.assertFalse(checks["confirmed_fixed"])

    def test_no_backlog_needs_the_worker_to_report_the_fixed_position(self):
        idle = healthy()
        idle["source"]["source_slot_confirmed_flush_lsn"] = "0/6000000"
        baseline = resume.baseline_of(idle)
        state = caught_up()
        state["source"]["source_slot_confirmed_flush_lsn"] = "0/6000000"
        state["target"]["received_lsn"] = None
        self.assertFalse(resume.assess(baseline, state, rows_arrived=True)["confirmed_flush_advanced"])
        state["target"]["received_lsn"] = "0/6000000"
        self.assertTrue(resume.assess(baseline, state, rows_arrived=True)["confirmed_flush_advanced"])

    def test_the_applied_position_is_required_only_once_a_marker_must_have_moved_it(self):
        # A subscriber's origin moves only on a commit. On a quiet source, WAL alone leaves it behind for ever.
        state = caught_up()
        state["target"]["target_origin_remote_lsn"] = "0/1000000"
        self.assertNotIn("applied_fixed", self.checks(state))
        floor = resume.parse_lsn("0/6000200")
        self.assertFalse(self.checks(state, applied_floor=floor)["applied_fixed"])
        state["target"]["target_origin_remote_lsn"] = "0/6000200"
        self.assertTrue(self.checks(state, applied_floor=floor)["applied_fixed"])
        state["target"]["target_origin_remote_lsn"] = None
        self.assertFalse(self.checks(state, applied_floor=floor)["applied_fixed"])

    def test_any_new_error_or_conflict_fails(self):
        for field in ("target_apply_errors", "target_sync_errors", "target_conflicts"):
            with self.subTest(field=field):
                state = caught_up()
                state["target"][field] += 1
                self.assertFalse(self.checks(state)["errors_not_increased"])

    def test_tables_still_copying_are_not_caught_up(self):
        state = caught_up()
        state["target"]["table_sync_states"]["public.messages"] = "d"
        self.assertFalse(self.checks(state)["tables_ready"])

    def test_the_single_writer_rule_is_rechecked_at_the_end(self):
        for side, field, value in (("target", "target_app_login", True), ("target", "app_sessions", 1),
                                   ("source", "source_app_login", False), ("source", "reverse_enabled", True)):
            with self.subTest(field=field):
                state = caught_up()
                state[side][field] = value
                self.assertFalse(self.checks(state)["single_writer_kept"])

    def test_a_vanished_subscription_or_slot_is_false_rather_than_a_crash(self):
        state = caught_up()
        state["target"].update(subscription_exists=False, forward_enabled=None, target_apply_errors=None,
                               target_sync_errors=None, target_conflicts=None, table_sync_states=None)
        self.assertFalse(self.checks(state)["forward_enabled"])
        self.assertFalse(self.checks(state)["errors_not_increased"])
        state = caught_up()
        state["source"].update(slot_exists=False, source_slot_invalidated=None, source_slot_confirmed_flush_lsn=None)
        self.assertFalse(self.checks(state)["source_slot_valid"])

    def test_rows_are_compared_against_the_source_snapshot(self):
        rows = resume.rows_arrived
        snapshot = healthy()["source_rows"]
        self.assertTrue(rows(snapshot, {"messages": 10, "messages_max_id": 10, "attachments": 2}))
        self.assertTrue(rows(snapshot, {"messages": 12, "messages_max_id": 12, "attachments": 3}))
        self.assertFalse(rows(snapshot, {"messages": 9, "messages_max_id": 10, "attachments": 2}))
        self.assertFalse(rows(snapshot, {"messages": 10, "messages_max_id": 9, "attachments": 2}))
        self.assertFalse(rows(snapshot, {"messages": 10, "messages_max_id": 10, "attachments": 1}))
        self.assertFalse(rows(snapshot, {"messages": 0, "messages_max_id": None, "attachments": 2}))

    def test_an_empty_source_needs_no_rows(self):
        empty = {"messages": 0, "messages_max_id": None, "attachments": 0}
        self.assertTrue(resume.rows_arrived(empty, {"messages": 0, "messages_max_id": None, "attachments": 0}))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PostCheckTests(unittest.TestCase):
    def run_loop(self, reads, *, timeout=10, settle=2, poll=1, firsts=None, applied_floor=None):
        clock = Clock()
        sequence = iter(reads)
        last = {}

        def status():
            value = next(sequence, last.get("value"))
            if isinstance(value, Exception):
                raise value
            last["value"] = value
            return value

        return resume.post_check(resume.baseline_of(healthy()), status, lambda: caught_up()["target_rows"], firsts=firsts,
                                 applied_floor=applied_floor, timeout=timeout, settle=settle, poll=poll, clock=clock, sleep=clock.sleep)

    def test_passes_after_staying_good_for_the_settle_period(self):
        result = self.run_loop([starting(), caught_up()])
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "confirmed")
        self.assertGreaterEqual(result["seconds"], 2)
        self.assertEqual(result["confirmed_fixed_seconds"], 1)
        self.assertEqual(result["positions"], {"fixed": "0/6000000", "source_slot_confirmed_flush_lsn": "0/6000100",
                                               "latest_end_lsn": "0/6000100", "target_origin_remote_lsn": "0/6000200"})

    def test_it_notes_when_each_position_was_first_seen_and_the_sample_before_it(self):
        firsts = {}
        received = starting()
        received["target"].update(apply_workers=1, worker_pid=1, received_lsn="0/6000100")
        self.run_loop([starting(), received, caught_up()], firsts=firsts)
        self.assertEqual(firsts["worker"]["elapsed_s"], 1)
        self.assertEqual(firsts["worker"]["previous_sample_s"], 0)
        self.assertEqual(firsts["received_fixed"]["elapsed_s"], 1)
        self.assertEqual(firsts["applied_fixed"]["elapsed_s"], 2)
        self.assertEqual(firsts["applied_fixed"]["db_utc"], "2026-11-04T07:26:46+00:00")

    def test_a_later_phase_continues_from_the_last_sample_of_the_one_before(self):
        firsts = {}
        behind = caught_up()
        behind["target"]["target_origin_remote_lsn"] = None
        earlier = self.run_loop([behind], settle=0)
        self.assertEqual(earlier["last_sample_s"], 0)
        clock = Clock()
        resume.post_check(resume.baseline_of(healthy()), caught_up, lambda: caught_up()["target_rows"], firsts=firsts,
                          previous_sample=9.5, timeout=3, settle=0, poll=1, clock=clock, sleep=clock.sleep)
        self.assertEqual(firsts["applied_fixed"]["previous_sample_s"], 9.5)

    def test_the_first_sample_has_no_sample_before_it(self):
        firsts = {}
        self.run_loop([caught_up()], firsts=firsts)
        self.assertIsNone(firsts["worker"]["previous_sample_s"])

    def test_stops_at_once_when_the_subscription_turns_itself_off_again(self):
        off = caught_up()
        off["target"]["forward_enabled"] = False
        result = self.run_loop([off], timeout=600)
        self.assertEqual((result["passed"], result["reason"]), (False, "failed_check"))
        self.assertLess(result["seconds"], 5)

    def test_stops_at_once_on_a_new_error(self):
        errored = caught_up()
        errored["target"]["target_apply_errors"] = 2
        result = self.run_loop([errored], timeout=600)
        self.assertEqual((result["passed"], result["reason"]), (False, "failed_check"))

    def test_stops_at_once_when_the_slot_is_invalidated_during_catch_up(self):
        lost = caught_up()
        lost["source"]["source_slot_invalidated"] = True
        result = self.run_loop([lost], timeout=600)
        self.assertEqual((result["passed"], result["reason"]), (False, "failed_check"))

    def test_no_worker_until_the_deadline_is_a_failure(self):
        waiting = caught_up()
        waiting["target"]["apply_workers"] = 0
        result = self.run_loop([waiting], timeout=5)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertFalse(result["checks"]["apply_worker_running"])

    def test_reads_that_keep_failing_are_a_failure_not_a_pass(self):
        result = self.run_loop([OSError(f"host=target.invalid password={SECRET}")] * 20, timeout=5)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertEqual(result["read_errors"], {"OSError": 6})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_a_brief_read_failure_does_not_end_the_check(self):
        self.assertTrue(self.run_loop([OSError("blip"), caught_up()])["passed"])

    def test_the_settle_period_restarts_when_the_worker_goes_away(self):
        gone = caught_up()
        gone["target"]["apply_workers"] = 0
        result = self.run_loop([caught_up(), gone, caught_up()], settle=2)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["seconds"], 4)

    def test_it_reports_every_worker_it_saw(self):
        second = caught_up()
        second["target"]["worker_pid"] = 4343
        self.assertEqual(self.run_loop([caught_up(), second], settle=1)["worker_pids_seen"], 2)

    def test_rows_that_never_arrive_fail_at_the_deadline(self):
        clock = Clock()
        short = {"messages": 1, "messages_max_id": 1, "attachments": 2}
        result = resume.post_check(resume.baseline_of(healthy()), caught_up, lambda: short,
                                   timeout=4, settle=1, poll=1, clock=clock, sleep=clock.sleep)
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertFalse(result["checks"]["rows_arrived"])

    def test_the_applied_floor_is_enforced_when_given(self):
        behind = caught_up()
        behind["target"]["target_origin_remote_lsn"] = "0/1000000"
        result = self.run_loop([behind], timeout=3, applied_floor=resume.parse_lsn("0/6000200"))
        self.assertEqual((result["passed"], result["reason"]), (False, "deadline"))
        self.assertFalse(result["checks"]["applied_fixed"])


MARKER_ROW = {"barrier_id": "id-1", "epoch": 0, "created_at": "t"}


class BarrierProbeTests(unittest.TestCase):
    def probe(self, target_rows, *, refused=(), answers_lost=(), source_unreadable=False, timeout=3, on_delivered=None, on_cleaned=None,
              applied=None):
        """refused never reaches the database. answers_lost is applied there and then the client hears nothing."""
        clock = Clock()
        sequence = iter(target_rows)
        answers_lost = list(answers_lost)
        source = {"row": None}
        writes, stages = [], []

        def read_target():
            value = next(sequence, target_rows[-1])
            if isinstance(value, Exception):
                raise value
            return value

        def read_source():
            if source_unreadable:
                raise OSError("source unreadable")
            return source["row"]

        def write(statement, params):
            kind = statement.split()[0]
            writes.append((kind, params))
            if kind in refused:
                raise refused[kind] if isinstance(refused, dict) else OSError("refused")
            source["row"] = MARKER_ROW if kind == "INSERT" else None
            if kind in answers_lost:
                answers_lost.remove(kind)
                raise OSError("server closed the connection")
            return resume.MarkerWrite("0/1C0001B0", "0/1C000200") if kind == "INSERT" else resume.MarkerWrite("0/1C000268", "0/1C000270")

        report = resume.barrier_probe(write, read_target, read_source, "id-1",
                                      on_delivered=on_delivered or (lambda: stages.append("delivered")),
                                      on_cleaned=on_cleaned or (lambda: stages.append("cleaned")),
                                      elapsed=clock, timeout=timeout, poll=1, sleep=clock.sleep,
                                      read_target_applied=(lambda: applied) if applied else None)
        self.source_row_left = source["row"]
        return report, [kind for kind, _ in writes], stages, writes

    def test_row_arrives_and_its_delete_follows(self):
        report, kinds, stages, writes = self.probe([None, MARKER_ROW, MARKER_ROW, None])
        self.assertTrue(report["passed"])
        self.assertEqual(report["barrier"]["elapsed_s"], 1)
        self.assertEqual(report["barrier"]["previous_sample_s"], 0)
        self.assertEqual(report["delete_arrived"]["elapsed_s"], 1)
        self.assertEqual((report["source_insert_submissions"], report["source_insert_commits"]), (1, 1))
        self.assertEqual((report["source_delete_submissions"], report["source_delete_commits"]), (1, 1))
        self.assertEqual((report["insert_lsn_before_commit"], report["delete_lsn_before_commit"]), ("0/1C0001B0", "0/1C000268"))
        self.assertTrue(report["same_row_on_both_sides_when_present"])
        self.assertEqual(report["cleanup"], "confirmed")
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertEqual(writes[0][1], ("id-1", resume.PROBE_EPOCH))
        self.assertEqual(writes[1][1], ("id-1", resume.PROBE_EPOCH))
        self.assertEqual(stages, ["delivered", "cleaned"])

    def test_a_row_that_never_arrives_fails_and_is_still_deleted_from_the_source(self):
        report, kinds, stages, _ = self.probe([None])
        self.assertFalse(report["passed"])
        self.assertIsNone(report["barrier"])
        self.assertEqual(report["source_delete_commits"], 1)
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertEqual(stages, [])
        self.assertIsNone(self.source_row_left)

    def test_a_delete_that_never_arrives_leaves_the_cleanup_pending_on_the_target(self):
        report, _, stages, _ = self.probe([MARKER_ROW])
        self.assertFalse(report["passed"])
        self.assertIsNotNone(report["barrier"])
        self.assertIsNone(report["delete_arrived"])
        self.assertEqual(stages, ["delivered"])
        self.assertEqual((report["cleanup"], report["pending_on"]), ("pending", ["target"]))
        self.assertIn("--cleanup-marker id-1", report["resume"])

    def test_a_different_row_on_the_target_fails(self):
        report, _, _, _ = self.probe([{**MARKER_ROW, "epoch": 9}, {**MARKER_ROW, "epoch": 9}, None])
        self.assertFalse(report["passed"])
        self.assertFalse(report["same_row_on_both_sides_when_present"])

    def test_an_absent_unconfirmed_insert_is_not_reported_as_uncommitted(self):
        class Denied(Exception):
            sqlstate = "42501"

        report, kinds, _, _ = self.probe([None], refused={"INSERT": Denied(f"password={SECRET}")})
        self.assertFalse(report["passed"])
        self.assertEqual((report["source_insert_submissions"], report["source_insert_commits"]), (1, None))
        self.assertEqual(report["source_delete_submissions"], 1)
        self.assertEqual(report["cleanup"], "pending")
        self.assertEqual(report["pending_on"], ["target"])
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertEqual((report["error_category"], report["sqlstate"]), ("Denied", "42501"))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_a_commit_whose_answer_was_lost_is_found_by_reading_and_removed(self):
        report, kinds, stages, _ = self.probe([None], answers_lost=["INSERT"], applied="0/1C000270")
        self.assertEqual((report["source_insert_submissions"], report["source_insert_commits"]), (1, 1))
        self.assertIsNone(report["insert_lsn_before_commit"])
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertIsNone(self.source_row_left)
        self.assertEqual(report["cleanup"], "confirmed")
        self.assertEqual(report["error_category"], "OSError")
        self.assertFalse(report["passed"])
        self.assertEqual(stages, [])

    def test_absent_target_before_apply_reaches_delete_is_still_pending(self):
        for applied in (None, "0/1C000200", "0/1C000268", "0/1C00026F"):
            with self.subTest(applied=applied):
                report, _, _, _ = self.probe([None], answers_lost=["INSERT"], applied=applied)
                self.assertEqual(report["cleanup"], "pending")
                self.assertEqual(report["pending_on"], ["target"])
                self.assertIsNone(report["delete_arrived"])

    def test_the_insert_is_never_sent_twice(self):
        for case in ({"answers_lost": ["INSERT"]}, {"refused": ("INSERT",)}, {"answers_lost": ["INSERT"], "refused": ("DELETE",)},
                     {"answers_lost": ["INSERT"], "source_unreadable": True}):
            with self.subTest(case=case):
                _, kinds, _, _ = self.probe([None], **case)
                self.assertEqual(kinds.count("INSERT"), 1)

    def test_an_unreadable_source_leaves_the_commit_unknown_and_the_cleanup_pending(self):
        report, kinds, _, _ = self.probe([None], answers_lost=["INSERT"], source_unreadable=True)
        self.assertIsNone(report["source_insert_commits"])
        self.assertEqual(report["cleanup"], "pending")
        self.assertIn("source", report["pending_on"])
        self.assertIn("--cleanup-marker id-1", report["resume"])
        self.assertFalse(report["passed"])
        self.assertEqual(kinds, ["INSERT"])

    def test_a_delete_that_keeps_failing_leaves_the_cleanup_pending_on_the_source(self):
        report, kinds, _, _ = self.probe([None], answers_lost=["INSERT"], refused=("DELETE",))
        self.assertEqual(report["source_insert_commits"], 1)
        self.assertEqual(report["source_delete_commits"], 0)
        self.assertGreaterEqual(report["source_delete_submissions"], 1)
        self.assertEqual(report["cleanup"], "pending")
        self.assertIn("source", report["pending_on"])
        self.assertEqual(self.source_row_left, MARKER_ROW)
        self.assertEqual(kinds[0], "INSERT")

    def test_a_delete_whose_answer_was_lost_is_confirmed_by_reading(self):
        report, _, _, _ = self.probe([MARKER_ROW, MARKER_ROW, None], answers_lost=["DELETE"])
        self.assertEqual(report["source_delete_commits"], 1)
        self.assertEqual(report["cleanup"], "confirmed")
        self.assertIsNone(self.source_row_left)
        self.assertFalse(report["passed"])

    def test_a_failure_after_arrival_still_deletes_the_row_from_the_source(self):
        def fails():
            raise OSError(f"password={SECRET}")

        report, kinds, _, _ = self.probe([MARKER_ROW, MARKER_ROW, None], on_delivered=fails)
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertEqual(report["source_delete_commits"], 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["error_category"], "OSError")
        self.assertNotIn(SECRET, json.dumps(report))

    def test_a_failure_after_the_delete_arrived_fails_the_probe(self):
        def fails():
            raise OSError("late")

        report, kinds, _, _ = self.probe([MARKER_ROW, MARKER_ROW, None], on_cleaned=fails)
        self.assertEqual(kinds, ["INSERT", "DELETE"])
        self.assertFalse(report["passed"])

    def test_a_read_failure_counts_as_not_yet(self):
        report, _, _, _ = self.probe([OSError("blip"), MARKER_ROW, MARKER_ROW, OSError("blip"), None])
        self.assertTrue(report["passed"])


class FakeWriteConnection:
    def __init__(self, identity):
        self.identity = identity
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def commit(self):
        self.statements.append("COMMIT")

    def execute(self, statement, params=None):
        self.statements.append(statement)
        value = self.identity if statement == resume.PROBE_IDENTITY else ("0/1C0001B0",)
        return types.SimpleNamespace(fetchone=lambda: value)


class WriteMarkerTests(unittest.TestCase):
    IDENTITY = types.MappingProxyType({"system_identifier": SOURCE_ID, "database": "chat"})

    def test_it_writes_only_after_the_same_session_shows_the_verified_source(self):
        connection = FakeWriteConnection((SOURCE_ID, "chat"))
        with patch.object(resume, "connect", return_value=connection):
            position = resume.write_marker("dsn", resume.PROBE_INSERT, ("id", 0), self.IDENTITY)
        self.assertEqual(position, resume.MarkerWrite("0/1C0001B0", "0/1C0001B0"))
        self.assertEqual(connection.statements[0], resume.PROBE_IDENTITY)
        self.assertEqual(connection.statements[1], resume.MARKER_LOCK)
        self.assertEqual(connection.statements[2], resume.PROBE_INSERT)
        self.assertEqual(connection.statements[4:6], ["COMMIT", resume.PROBE_IDENTITY])

    def test_a_session_on_another_cluster_or_database_writes_nothing(self):
        for seen in ((TARGET_ID, "chat"), (SOURCE_ID, "other")):
            with self.subTest(seen=seen):
                connection = FakeWriteConnection(seen)
                with patch.object(resume, "connect", return_value=connection), self.assertRaises(resume.ProbeTargetChanged):
                    resume.write_marker("dsn", resume.PROBE_INSERT, ("id", 0), self.IDENTITY)
                self.assertNotIn(resume.PROBE_INSERT, connection.statements)


def fake_read(state):
    """Reads a prepared state; the first argument is which database the DSN named."""
    def read(dsn, name, params=None):
        if name == "business_tables":
            return copy.deepcopy(TABLES)
        if name == "barrier_row":
            return None
        # Copies, as a real read returns a new value each time.
        if "source.invalid" in dsn:
            return copy.deepcopy(state["source_rows"] if name == "source_rows" else state["source"])
        if "target.invalid" in dsn:
            if name == "target_origin":
                return state["target"]["target_origin_remote_lsn"]
            return copy.deepcopy(state["target_rows"] if name == "target_rows" else state["target"])
        if "probe.invalid" in dsn:
            return state["probe"]
        return {key: state["executor"].get(key, 0) for key in ("holding", "executions", "leases", "resource_fences")}
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

    def test_the_applied_position_is_read_only_where_the_role_may(self):
        order = []
        state = changed(["target", "origin_readable"], False)

        def read(dsn, name, params=None):
            order.append(name)
            return fake_read(state)(dsn, name, params)

        with patch.object(resume, "read_one", side_effect=read):
            resume.read_status(ENVIRON["T8_RESUME_SOURCE_DSN"], ENVIRON["T8_RESUME_TARGET_DSN"])
        self.assertNotIn("target_origin", order)


class RunTests(unittest.TestCase):
    def run_tool(self, state, *arguments, environ=ENVIRON):
        def write(_dsn, statement, _params, _identity):
            return resume.MarkerWrite("0/6000300", "0/6000301") if statement.startswith("INSERT") else resume.MarkerWrite("0/6000400", "0/6000401")

        with (patch.object(resume, "read_one", side_effect=fake_read(state)), patch.object(resume, "enable") as enable,
              patch.object(resume, "write_marker", side_effect=write) as marker):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(list(arguments), environ)
        self.marker = marker
        return code, json.loads(buffer.getvalue()), enable

    def test_dry_run_is_the_default_and_never_enables(self):
        code, report, enable = self.run_tool(healthy())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dry-run")
        self.assertIn("t8_source_to_target", report["would_run"])
        enable.assert_not_called()
        self.marker.assert_not_called()

    def test_the_default_reports_the_gap_the_way_the_first_resume_did(self):
        _, report, _ = self.run_tool(healthy())
        self.assertEqual(report["immediately_before_enable"]["gap_bytes"], 0x5000000)
        self.assertEqual(report["stages"]["before"]["target_apply_errors"], 1)
        self.assertEqual(report["stages"]["before"]["business_tables"]["messages"]["source_rows"], 10)
        self.assertEqual(report["continuity"]["source_wal_sender_timeout_ms"], 5000)
        self.assertEqual(report["continuity"]["target_wal_receiver_timeout_ms"], 5000)
        self.assertTrue(report["continuity"]["disable_on_error"])

    def test_refused_dry_run_exits_nonzero_and_names_the_reason(self):
        code, report, enable = self.run_tool(changed(["source", "reverse_enabled"], True))
        self.assertEqual(code, 1)
        self.assertIn("reverse_active", report["refusals"])
        self.assertNotIn("would_run", report)
        enable.assert_not_called()

    def test_refused_apply_never_enables(self):
        code, report, enable = self.run_tool(changed(["target", "target_app_login"], True), "--apply", *PINS)
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "preflight")
        enable.assert_not_called()

    def apply_flow(self, *arguments):
        """--apply with the state moving: off before ENABLE, caught up after it."""
        state = {"value": healthy()}
        events = []

        def read(dsn, name, params=None):
            if name == "barrier_row":
                present = events.count("insert") and not events.count("delete")
                return {"barrier_id": "x"} if present else None
            return fake_read(state["value"])(dsn, name, params)

        def enable(*_):
            events.append("enable")
            state["value"] = caught_up()

        def write(_dsn, statement, _params, identity):
            self.written_to = identity
            events.append("insert" if statement.startswith("INSERT") else "delete")
            return resume.MarkerWrite("0/6000180", "0/6000181") if statement.startswith("INSERT") else resume.MarkerWrite("0/6000190", "0/6000191")

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable),
              patch.object(resume, "write_marker", side_effect=write)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS, *FAST, *arguments], ENVIRON)
        return code, json.loads(buffer.getvalue()), events

    def test_apply_enables_once_then_sends_the_marker_through(self):
        code, report, events = self.apply_flow()
        self.assertEqual(code, 0, report)
        self.assertEqual(events, ["enable", "insert", "delete"])
        self.assertEqual(report["enabled_by"], "alter_subscription")
        self.assertEqual(report["continuity"]["enable_submissions"], 1)
        self.assertEqual(sorted(report["stages"]), ["before", "cleaned", "delivered"])
        self.assertTrue(report["catch_up"]["passed"])
        self.assertTrue(report["post_check"]["passed"])
        self.assertTrue(report["post_check"]["checks"]["applied_fixed"])
        self.assertEqual(report["marker"]["source_insert_commits"], 1)
        self.assertIn("barrier", report["command_clock"]["first_observations"])
        self.assertIsNotNone(report["command_clock"]["enable_command_return_s"])
        self.assertTrue(report["stages"]["cleaned"]["business_tables"]["messages"]["sha256_equal"])

    def test_the_marker_is_written_only_to_the_identity_of_the_verified_source(self):
        code, report, _ = self.apply_flow()
        self.assertEqual(code, 0, report)
        self.assertEqual(self.written_to, {"system_identifier": SOURCE_ID, "database": "chat"})

    def test_a_probe_dsn_elsewhere_stops_before_enable_and_before_the_marker(self):
        for path, value, reason in ((["probe", "system_identifier"], TARGET_ID, "barrier_probe_wrong_database"),
                                    (["probe", "database"], "other", "barrier_probe_wrong_database")):
            with self.subTest(value=value):
                code, report, enable = self.run_tool(changed(path, value), "--apply", *PINS)
                self.assertEqual(code, 1)
                self.assertIn(reason, report["refusals"])
                enable.assert_not_called()
                self.marker.assert_not_called()

    def test_a_probe_dsn_that_cannot_be_read_stops_before_enable_and_before_the_marker(self):
        state = healthy()
        real = fake_read(state)

        def read(dsn, name, params=None):
            if "probe.invalid" in dsn:
                raise OSError(f"password={SECRET}")
            return real(dsn, name, params)

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable") as enable,
              patch.object(resume, "write_marker") as marker):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertIn("barrier_probe_unconfirmed", report["refusals"])
        self.assertEqual(report["errors"]["probe"], "OSError")
        self.assertNotIn(SECRET, buffer.getvalue())
        enable.assert_not_called()
        marker.assert_not_called()

    def test_a_cleanup_left_pending_says_how_to_resume(self):
        def lost(_dsn, _statement, _params, _identity):
            raise OSError("server closed the connection")

        state = {"value": healthy()}
        reads = {"source": {"barrier_id": "x", "epoch": 0}}

        def read(dsn, name, params=None):
            if name == "barrier_row":
                return reads["source"] if "source.invalid" in dsn else None
            return fake_read(state["value"])(dsn, name, params)

        def enable(*_):
            state["value"] = caught_up()

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable),
              patch.object(resume, "write_marker", side_effect=lost)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS, *FAST, "--probe-timeout", "0"], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "marker")
        self.assertEqual(report["marker"]["cleanup"], "pending")
        self.assertIn(f"--cleanup-marker {report['marker']['barrier_id']}", report["marker"]["resume"])

    def test_the_marker_goes_in_before_the_settle_period_so_it_is_settled_too(self):
        order = []
        real = resume.post_check

        def spy(*args, **kwargs):
            order.append(("check", kwargs["settle"]))
            return real(*args, **kwargs)

        with patch.object(resume, "post_check", side_effect=spy):
            self.apply_flow("--settle", "0.01")
        self.assertEqual(order, [("check", 0), ("check", 0.01)])

    def test_without_origin_access_the_round_trip_is_the_proof(self):
        state = {"value": changed(["target", "origin_readable"], False)}
        events = []

        def read(dsn, name, params=None):
            if name == "barrier_row":
                return {"barrier_id": "x"} if events.count("insert") and not events.count("delete") else None
            return fake_read(state["value"])(dsn, name, params)

        def enable(*_):
            after = caught_up()
            after["target"].update(origin_readable=False, target_origin_remote_lsn=None)
            state["value"] = after

        def write(_dsn, statement, _params, _identity):
            events.append("insert" if statement.startswith("INSERT") else "delete")
            return resume.MarkerWrite("0/6000190", "0/6000191")

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable),
              patch.object(resume, "write_marker", side_effect=write)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS, *FAST], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, report)
        self.assertNotIn("applied_fixed", report["post_check"]["checks"])
        self.assertEqual(report["continuity"]["applied_position"], "implied_by_marker")

    def test_the_applied_position_is_read_when_the_role_may(self):
        _, report, _ = self.apply_flow()
        self.assertEqual(report["continuity"]["applied_position"], "read_from_origin")

    def test_no_marker_means_a_plain_final_stage_and_no_applied_requirement(self):
        code, report, events = self.apply_flow("--no-barrier-probe")
        self.assertEqual(code, 0, report)
        self.assertEqual(events, ["enable"])
        self.assertEqual(sorted(report["stages"]), ["before", "final"])
        self.assertNotIn("applied_fixed", report["post_check"]["checks"])
        self.assertEqual(report["marker"], {"state": "skipped_by_operator"})

    def test_apply_on_a_running_subscription_never_enables_again(self):
        code, report, enable = self.run_tool(caught_up(), "--apply", "--no-barrier-probe", *PINS, *FAST)
        enable.assert_not_called()
        self.assertIsNone(report["enabled_by"])
        self.assertEqual(report["continuity"]["enable_submissions"], 0)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["post_check"]["passed"])

    def test_the_default_on_a_running_subscription_runs_the_read_only_post_check(self):
        code, report, enable = self.run_tool(caught_up(), *FAST)
        self.assertEqual(code, 0, report)
        self.assertIsNone(report["would_run"])
        self.assertTrue(report["post_check"]["passed"])
        self.assertEqual(report["marker"], {"state": "not_run_without_apply"})
        enable.assert_not_called()
        self.marker.assert_not_called()

    def test_the_default_needs_no_privilege_on_a_running_subscription(self):
        state = caught_up()
        state["target"].update(can_alter=False, can_call_control=False)
        code, report, _ = self.run_tool(state, *FAST)
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
        with patch.object(resume, "read_one") as read:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS], without("T8_RESUME_SOURCE_PROBE_DSN"))
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

    def test_the_source_write_block_is_only_ever_recorded(self):
        for argument, expected in ((("--source-write-block", "on"), "on"), (("--source-write-block", "off"), "off"), ((), "not_read")):
            with self.subTest(expected=expected):
                code, report, enable = self.run_tool(healthy(), *argument)
                self.assertEqual(code, 0)
                self.assertEqual(report["continuity"]["source_write_block_before"], expected)
                enable.assert_not_called()

    def test_a_failed_catch_up_never_writes_the_marker(self):
        before, off = healthy(), caught_up()
        off["target"]["forward_enabled"] = False
        state = {"value": before}

        def enable(*_):
            state["value"] = off

        def read(dsn, name, params=None):
            return fake_read(state["value"])(dsn, name, params)

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable),
              patch.object(resume, "write_marker") as marker):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", *PINS, *FAST], ENVIRON)
        self.assertEqual(code, 1)
        marker.assert_not_called()
        self.assertEqual(json.loads(buffer.getvalue())["stage"], "catch_up")

    def test_a_failed_marker_fails_the_run(self):
        with patch.object(resume, "barrier_probe", return_value={"passed": False, "delete_lsn_before_commit": None}):
            code, report, _ = self.apply_flow()
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "marker")

    def test_new_errors_after_the_marker_fail_the_run(self):
        results = iter([{"passed": True, "last_sample_s": 1}, {"passed": False}])
        with patch.object(resume, "post_check", side_effect=lambda *_a, **_k: next(results)):
            code, report, _ = self.apply_flow()
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "post_check")

    def test_an_execution_that_started_meanwhile_fails_the_run(self):
        reads = {"count": 0}
        state = caught_up()
        real = fake_read(state)

        def read(dsn, name, params=None):
            if name == "platform_counts":
                reads["count"] += 1
                holding = 0 if reads["count"] <= 2 else 1
                return {"holding": holding, "executions": holding, "leases": holding, "resource_fences": holding}
            return real(dsn, name, params)

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([*FAST], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "continuity")
        self.assertEqual(report["continuity"]["platform_before_after"]["executions"], [0, 1])

    def test_a_replaced_subscription_fails_the_run(self):
        state = caught_up()
        reads = {"count": 0}
        real = fake_read(state)

        def read(dsn, name, params=None):
            value = real(dsn, name, params)
            if name == "target_status":
                reads["count"] += 1
                # Twice for the preflight, once for the "before" stage, then the catch-up.
                return {**value, "subscription_oid": 16400 if reads["count"] <= 3 else 17000}
            return value

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([*FAST], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["continuity"]["same_subscription_oid_and_origin"])

    def test_hashes_that_differ_at_the_end_fail_the_run(self):
        state = caught_up()
        real = fake_read(state)

        def read(dsn, name, params=None):
            value = real(dsn, name, params)
            if name == "business_tables" and "target.invalid" in dsn:
                value["messages"]["sha256"] = "different"
            return value

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([*FAST, "--probe-timeout", "0"], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["stages"]["final"]["business_tables"]["messages"]["sha256_equal"])
        self.assertFalse(report["continuity"]["business_tables_match"])
        self.assertNotIn("different", json.dumps(report))

    def test_a_failure_after_enable_keeps_the_receipt(self):
        state = {"value": healthy()}
        reads = {"platform": 0}

        def read(dsn, name, params=None):
            if name == "platform_counts":
                reads["platform"] += 1
                if reads["platform"] > 2:
                    raise OSError(f"password={SECRET}")
            return fake_read(state["value"])(dsn, name, params)

        def enable(*_):
            state["value"] = caught_up()

        with patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--apply", "--no-barrier-probe", *PINS, *FAST], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["enabled_by"], "alter_subscription")
        self.assertEqual(report["continuity"]["enable_submissions"], 1)
        self.assertTrue(report["post_check"]["passed"])
        self.assertEqual(report["error_category"], "OSError")
        self.assertNotIn(SECRET, buffer.getvalue())

    def test_an_enable_that_raised_still_counts_as_submitted(self):
        # The server may have committed it before the connection failed.
        with (patch.object(resume, "read_one", side_effect=fake_read(healthy())),
              patch.object(resume, "enable", side_effect=OSError("lost"))):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                resume.main(["--apply", "--no-barrier-probe", *PINS], ENVIRON)
        self.assertEqual(json.loads(buffer.getvalue())["continuity"]["enable_submissions"], 1)

    def test_a_row_committed_between_the_two_hash_reads_does_not_fail_the_run(self):
        state = caught_up()
        reads = {"target": 0}
        real = fake_read(state)

        def read(dsn, name, params=None):
            value = real(dsn, name, params)
            if name == "business_tables" and "target.invalid" in dsn:
                reads["target"] += 1
                # The "before" and "final" stages, then one final comparison that lags, then it agrees.
                if reads["target"] == 3:
                    value["messages"]["sha256"] = "lagging"
            return value

        with patch.object(resume, "read_one", side_effect=read):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main([*FAST, "--probe-timeout", "5"], ENVIRON)
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 0, report)
        self.assertTrue(report["continuity"]["business_tables_match"])

    def test_a_failed_enable_is_reported_without_its_message(self):
        class Denied(Exception):
            sqlstate = "42501"

        with (patch.object(resume, "read_one", side_effect=fake_read(healthy())),
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
        code, report, _ = self.run_tool(healthy(), environ=without("T8_RESUME_EXECUTOR_DSN"))
        self.assertEqual(code, 1)
        self.assertIn("executor_state_unconfirmed", report["refusals"])

    def test_an_operator_can_declare_no_executor_and_the_receipt_says_so(self):
        code, report, _ = self.run_tool(healthy(), "--executor-not-deployed", environ=without("T8_RESUME_EXECUTOR_DSN"))
        self.assertEqual(code, 0)
        self.assertEqual(report["preflight"]["executor"], {"state": "declared_not_deployed"})

    def test_the_declaration_cannot_hide_a_running_executor(self):
        state = healthy()
        state["executor"] = {"state": "holding", "holding": 1, "executions": 1, "leases": 1, "resource_fences": 1}
        code, report, _ = self.run_tool(state, "--executor-not-deployed")
        self.assertEqual(code, 1)
        self.assertIn("executor_running", report["refusals"])

    def test_control_sessions_still_block_a_declared_absent_executor(self):
        code, report, _ = self.run_tool(changed(["source", "control_sessions"], 2), "--executor-not-deployed",
                                        environ=without("T8_RESUME_EXECUTOR_DSN"))
        self.assertEqual(code, 1)
        self.assertIn("control_session_present", report["refusals"])

    def test_the_report_never_carries_a_dsn(self):
        _, report, _ = self.run_tool(healthy())
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(".invalid", json.dumps(report))


class CleanupMarkerTests(unittest.TestCase):
    MARKER_ID = "2bfff017-b2b5-4e0c-a910-013b621cefef"

    def run_cleanup(self, *arguments, state=None, source_row=True, target_row=False, epoch=0, environ=ENVIRON, delete_works=True):
        state = state or caught_up()
        rows = {"source": {"barrier_id": self.MARKER_ID, "epoch": epoch} if source_row else None}
        target = {"row": {"barrier_id": self.MARKER_ID, "epoch": epoch} if target_row else None}
        real = fake_read(state)
        writes = []

        def read(dsn, name, params=None):
            if name == "barrier_row":
                return rows["source"] if "source.invalid" in dsn else target["row"]
            return real(dsn, name, params)

        def write(_dsn, statement, params, identity):
            writes.append((statement, params, identity))
            if not delete_works:
                raise OSError("refused")
            rows["source"] = None
            return resume.MarkerWrite("0/1", "0/2")

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable") as enable,
              patch.object(resume, "write_marker", side_effect=write)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(list(arguments), environ)
        enable.assert_not_called()
        return code, json.loads(buffer.getvalue()), writes

    def command(self, *extra):
        return ("--cleanup-marker", self.MARKER_ID, "--marker-protocol", resume.MARKER_PROTOCOL, *PINS, "--probe-poll", "0", "--probe-timeout", "0", *extra)

    def test_legacy_cleanup_without_serialization_receipt_is_refused(self):
        code, report, writes = self.run_cleanup("--cleanup-marker", self.MARKER_ID, *PINS)
        self.assertEqual(code, 1)
        self.assertEqual(report["refusals"], ["marker_protocol_unconfirmed"])
        self.assertEqual(writes, [])

    def test_it_removes_only_the_named_row_of_the_verified_source(self):
        code, report, writes = self.run_cleanup(*self.command())
        self.assertEqual(code, 0, report)
        self.assertEqual(report["mode"], "cleanup-marker")
        self.assertEqual(report["marker"]["cleanup"], "confirmed")
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], resume.PROBE_DELETE)
        self.assertEqual(writes[0][1], (self.MARKER_ID, resume.PROBE_EPOCH))
        self.assertEqual(writes[0][2], {"system_identifier": SOURCE_ID, "database": "chat"})

    def test_a_finished_cleanup_reads_as_passed_throughout(self):
        _, report, _ = self.run_cleanup(*self.command())
        self.assertTrue(report["passed"])
        self.assertTrue(report["marker"]["passed"])

    def test_the_cleanup_makes_no_claim_about_the_executor(self):
        _, report, _ = self.run_cleanup(*self.command())
        self.assertNotIn("executor", report["preflight"])

    def test_a_source_dsn_on_another_database_of_the_cluster_is_refused(self):
        code, report, writes = self.run_cleanup(*self.command(), state=changed(["source", "slot_database"], "other"))
        self.assertEqual(code, 1)
        self.assertIn("source_wrong_database", report["refusals"])
        self.assertEqual(writes, [])

    def test_an_absent_row_still_requires_serialized_cleanup(self):
        code, report, writes = self.run_cleanup(*self.command(), source_row=False)
        self.assertEqual(code, 0, report)
        self.assertEqual([w[0] for w in writes], [resume.PROBE_DELETE])
        self.assertIsNone(report["marker"]["source_insert_commits"])

    def test_a_row_of_another_execution_is_refused_and_kept(self):
        code, report, writes = self.run_cleanup(*self.command(), epoch=7)
        self.assertEqual(code, 1)
        self.assertEqual(report["refusals"], ["not_a_marker_row"])
        self.assertEqual(writes, [])

    def test_a_row_still_on_the_target_stays_pending_and_is_never_written_there(self):
        code, report, writes = self.run_cleanup(*self.command(), target_row=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["marker"]["pending_on"], ["target"])
        self.assertEqual([statement for statement, _, _ in writes], [resume.PROBE_DELETE])

    def test_a_delete_that_fails_stays_pending(self):
        code, report, _ = self.run_cleanup(*self.command(), delete_works=False)
        self.assertEqual(code, 1)
        self.assertEqual(report["marker"]["cleanup"], "pending")

    def test_it_needs_the_identifiers_and_a_probe_on_the_verified_source(self):
        code, report, writes = self.run_cleanup("--cleanup-marker", self.MARKER_ID, "--marker-protocol", resume.MARKER_PROTOCOL)
        self.assertEqual(code, 1)
        self.assertIn("source_identity_not_pinned", report["refusals"])
        self.assertEqual(writes, [])
        code, report, writes = self.run_cleanup(*self.command(), state=changed(["probe", "database"], "other"))
        self.assertEqual(code, 1)
        self.assertIn("barrier_probe_wrong_database", report["refusals"])
        self.assertEqual(writes, [])

    def test_an_id_that_is_not_a_uuid_is_refused_before_any_connection(self):
        with patch.object(resume, "read_one") as read:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--cleanup-marker", "x'; DROP", *PINS], ENVIRON)
        self.assertEqual(code, 1)
        read.assert_not_called()
        self.assertEqual(json.loads(buffer.getvalue())["refusals"], ["invalid_barrier_id"])

    def test_it_cannot_be_combined_with_apply(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            resume.main(["--apply", "--cleanup-marker", self.MARKER_ID, *PINS], ENVIRON)

    def test_a_missing_dsn_is_refused_before_any_connection(self):
        with patch.object(resume, "read_one") as read:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--cleanup-marker", self.MARKER_ID, *PINS], without("T8_RESUME_SOURCE_PROBE_DSN"))
        self.assertEqual(code, 1)
        read.assert_not_called()
        self.assertEqual(json.loads(buffer.getvalue())["refusals"], ["missing_T8_RESUME_SOURCE_PROBE_DSN"])


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

    def test_a_start_argument_can_be_left_out_so_that_alter_system_can_change_it(self):
        # A command-line setting outranks ALTER SYSTEM. Fixing the timeouts there made a 60 s trial run at 5 s.
        arguments = lab.server_arguments({"wal_level": "logical", "wal_receiver_timeout": None})
        self.assertEqual(arguments, ["-c", "wal_level=logical"])

    def test_lab_parameters_add_to_the_defaults_without_changing_them(self):
        before = dict(lab.DB_PARAMETERS)
        chosen = lab.Lab(None, parameters={"wal_receiver_timeout": "60s"}).parameters
        self.assertEqual(chosen["wal_receiver_timeout"], "60s")
        self.assertEqual(chosen["wal_level"], "logical")
        self.assertEqual(lab.DB_PARAMETERS, before)
        self.assertEqual(lab.Lab(None).parameters, before)


if __name__ == "__main__":
    unittest.main()
