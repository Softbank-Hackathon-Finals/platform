"""Resuming the reverse subscription after a cutover, with the forward command's rules and the databases swapped.

Inside the command "source" is always the publisher, the one database that takes business writes, and "target"
the subscriber. After a cutover that is the target database and the source database. The states below are written
in those roles; the report must still name the real databases.
"""
import copy
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resume_replication import (
    ENVIRON,
    FAST,
    PINS,
    SECRET,
    SOURCE_ID,
    TABLES,
    TARGET_ID,
    caught_up,
    healthy,
    resume,
)

REVERSE_ENVIRON = {**ENVIRON, "T8_RESUME_TARGET_PROBE_DSN": f"host=target-probe.invalid password={SECRET}"}
PUBLISHER_READS = ("source_status", "source_rows")
SUBSCRIBER_READS = ("target_status", "target_rows", "target_origin")


def as_reverse(state):
    """The publisher is the target database now, and the subscriber the source database."""
    state["source"]["system_identifier"], state["target"]["system_identifier"] = TARGET_ID, SOURCE_ID
    state["probe"]["system_identifier"] = TARGET_ID
    return state


def after_cutover():
    """Writes go to the target, the source app cannot log in, the forward is off, and the reverse turned itself off."""
    return as_reverse(healthy())


def changed(path, value):
    state = after_cutover()
    node = state
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return state


def fake_read(state, calls=None):
    """Each read answers only from the database that holds it, so a read sent to the wrong one fails the run."""
    def read(dsn, name, params=None):
        host = re.search(r"host=(\S+)", dsn).group(1)
        if calls is not None:
            calls.append((host, name, params))
        if name == "business_tables":
            return copy.deepcopy(TABLES)
        if name == "barrier_row":
            return None
        if name in PUBLISHER_READS and host == "target.invalid":
            return copy.deepcopy(state["source_rows"] if name == "source_rows" else state["source"])
        if name in SUBSCRIBER_READS and host == "source.invalid":
            if name == "target_origin":
                return state["target"]["target_origin_remote_lsn"]
            return copy.deepcopy(state["target_rows"] if name == "target_rows" else state["target"])
        if name == "probe_status" and host == "target-probe.invalid":
            return state["probe"]
        if name == "platform_counts" and host == "state.invalid":
            return {key: state["executor"].get(key, 0) for key in ("holding", "executions", "leases", "resource_fences")}
        raise LookupError(f"{name} is not held by {host}")
    return read


def run_tool(state, *arguments, environ=REVERSE_ENVIRON, calls=None):
    with (patch.object(resume, "read_one", side_effect=fake_read(state, calls)), patch.object(resume, "enable") as enable,
          patch.object(resume, "write_marker") as marker):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = resume.main(["--direction", "reverse", *arguments], environ)
    return code, json.loads(buffer.getvalue()), enable, marker


class ReverseDryRunTests(unittest.TestCase):
    def test_the_state_after_a_cutover_passes_and_only_the_reverse_is_named(self):
        code, report, enable, marker = run_tool(after_cutover())
        self.assertEqual(code, 0, report)
        self.assertEqual(report["direction"], "reverse")
        self.assertEqual(report["would_run"], 'ALTER SUBSCRIPTION "t8_target_to_source" ENABLE;')
        enable.assert_not_called()
        marker.assert_not_called()

    def test_the_control_function_names_the_reverse_too(self):
        state = changed(["target", "can_call_control"], True)
        _, report, _, _ = run_tool(state)
        self.assertEqual(report["would_run"], "SELECT cutover.set_subscription('t8_target_to_source', true)")

    def test_each_role_is_read_from_its_own_database_with_the_reverse_names(self):
        calls = []
        code, report, _, _ = run_tool(after_cutover(), calls=calls)
        self.assertEqual(code, 0, report)
        hosts = {name: {host for host, read, _ in calls if read == name} for name in ("source_status", "target_status")}
        self.assertEqual(hosts, {"source_status": {"target.invalid"}, "target_status": {"source.invalid"}})
        params = next(params for _, name, params in calls if name == "source_status")
        self.assertEqual(params, {"forward": "t8_target_to_source", "reverse": "t8_source_to_target", "publication": "target_chat"})

    def test_the_report_names_the_real_databases(self):
        _, report, _, _ = run_tool(after_cutover())
        preflight = report["preflight"]
        self.assertEqual(preflight["target"]["system_identifier"], TARGET_ID)
        self.assertEqual(preflight["source"]["system_identifier"], SOURCE_ID)
        # The reverse slot lives on the target and the reverse subscription on the source.
        self.assertIn("target_slot_active", preflight["target"])
        self.assertIn("forward_enabled", preflight["target"])
        self.assertIn("reverse_enabled", preflight["source"])
        self.assertIn("forward_slot_active", preflight["source"])
        self.assertEqual(preflight["target_rows"]["messages"], 10)
        continuity = report["continuity"]
        self.assertEqual(set(continuity) & {"target_wal_sender_timeout_ms", "source_wal_receiver_timeout_ms", "target_write_block_before"},
                         {"target_wal_sender_timeout_ms", "source_wal_receiver_timeout_ms", "target_write_block_before"})
        self.assertNotIn("source_write_block_before", continuity)
        self.assertIn("target_wal_flush_lsn", report["immediately_before_enable"])

    def test_without_the_option_the_forward_report_does_not_change(self):
        def forward(*arguments):
            from test_resume_replication import fake_read as forward_read
            with patch.object(resume, "read_one", side_effect=forward_read(healthy())), patch.object(resume, "enable"):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    resume.main(list(arguments), ENVIRON)
            return json.loads(buffer.getvalue())

        default = forward()
        self.assertNotIn("direction", default)
        self.assertEqual(forward("--direction", "forward"), default)
        self.assertIn("source_write_block_before", default["continuity"])


class ReverseRefusalTests(unittest.TestCase):
    def test_each_unsafe_state_is_refused_under_the_real_name(self):
        cases = {
            "source_app_can_login": changed(["target", "target_app_login"], True),
            "source_app_sessions": changed(["target", "app_sessions"], 1),
            "target_app_cannot_login": changed(["source", "source_app_login"], False),
            "forward_active": changed(["source", "reverse_enabled"], True),
            "forward_slot_in_use": changed(["target", "reverse_slot_active"], True),
            "slot_in_use": changed(["source", "source_slot_active"], True),
            "slot_at_risk": changed(["source", "source_slot_wal_status"], "unreserved"),
            "source_ahead_of_target": changed(["target_rows", "messages_max_id"], 11),
            "target_is_not_primary": changed(["source", "in_recovery"], True),
            "executor_running": changed(["executor"], {"state": "holding", "holding": 1}),
        }
        for reason, state in cases.items():
            with self.subTest(reason=reason):
                code, report, enable, _ = run_tool(state)
                self.assertEqual(code, 1)
                self.assertIn(reason, report["refusals"])
                enable.assert_not_called()

    def test_a_running_forward_worker_counts_even_if_the_flag_says_off(self):
        _, report, _, _ = run_tool(changed(["source", "reverse_workers"], 1))
        self.assertIn("forward_active", report["refusals"])

    def test_the_resting_state_before_a_cutover_is_refused(self):
        # Source writing, target closed, forward running: the reverse must not start.
        resting = after_cutover()
        resting["source"].update(source_app_login=False, reverse_enabled=True, reverse_workers=1)
        resting["target"].update(target_app_login=True, reverse_slot_active=True)
        _, report, enable, _ = run_tool(resting)
        for reason in ("target_app_cannot_login", "source_app_can_login", "forward_active", "forward_slot_in_use"):
            self.assertIn(reason, report["refusals"])
        enable.assert_not_called()

    def test_the_source_probe_dsn_cannot_stand_in_for_the_target_one(self):
        environ = {key: value for key, value in REVERSE_ENVIRON.items() if key != "T8_RESUME_TARGET_PROBE_DSN"}
        code, report, enable, marker = run_tool(after_cutover(), "--apply", *PINS, environ=environ)
        self.assertEqual(code, 1)
        self.assertEqual(report["refusals"], ["missing_T8_RESUME_TARGET_PROBE_DSN"])
        enable.assert_not_called()
        marker.assert_not_called()

    def test_pins_name_the_real_databases(self):
        swapped = ("--source-system-id", TARGET_ID, "--target-system-id", SOURCE_ID)
        _, report, enable, _ = run_tool(after_cutover(), "--apply", *swapped)
        self.assertIn("source_identity_mismatch", report["refusals"])
        self.assertIn("target_identity_mismatch", report["refusals"])
        enable.assert_not_called()

    def test_each_write_block_option_belongs_to_its_direction(self):
        for arguments in (["--direction", "reverse", "--source-write-block", "off"], ["--target-write-block", "off"]):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as stop:
                resume.main(arguments, REVERSE_ENVIRON)
            self.assertEqual(stop.exception.code, 2)


class ReverseApplyTests(unittest.TestCase):
    def apply_flow(self, after_enable, *arguments, delete_never_arrives=False):
        state = {"value": after_cutover()}
        events, written = [], []
        self.calls, self.enabled_with = [], []

        def read(dsn, name, params=None):
            if name == "barrier_row":
                on_subscriber = "source.invalid" in dsn
                present = events.count("insert") and (not events.count("delete") or (delete_never_arrives and on_subscriber))
                return {"barrier_id": "x"} if present else None
            return fake_read(state["value"], self.calls)(dsn, name, params)

        def enable(dsn, method, direction):
            events.append(("enable", re.search(r"host=(\S+)", dsn).group(1)))
            self.enabled_with.append(resume.enable_statement(method, direction))
            state["value"] = after_enable()

        def write(dsn, statement, _params, identity):
            written.append((re.search(r"host=(\S+)", dsn).group(1), identity))
            events.append("insert" if statement.startswith("INSERT") else "delete")
            return resume.MarkerWrite("0/6000180", "0/6000181") if statement.startswith("INSERT") else resume.MarkerWrite("0/6000190", "0/6000191")

        with (patch.object(resume, "read_one", side_effect=read), patch.object(resume, "enable", side_effect=enable),
              patch.object(resume, "write_marker", side_effect=write)):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = resume.main(["--direction", "reverse", "--apply", *PINS, *FAST, "--target-write-block", "off", *arguments],
                                   REVERSE_ENVIRON)
        return code, json.loads(buffer.getvalue()), events, written

    def test_apply_enables_the_reverse_on_the_source_and_proves_the_catch_up(self):
        code, report, events, _ = self.apply_flow(lambda: as_reverse(caught_up()))
        self.assertEqual(code, 0, report)
        self.assertEqual(events, [("enable", "source.invalid"), "insert", "delete"])
        self.assertEqual(self.enabled_with, ['ALTER SUBSCRIPTION "t8_target_to_source" ENABLE;'])
        self.assertTrue(report["post_check"]["passed"])
        self.assertTrue(report["post_check"]["checks"]["reverse_enabled"])
        self.assertTrue(report["post_check"]["checks"]["target_slot_valid"])
        self.assertEqual(report["marker"]["target_insert_commits"], 1)
        self.assertEqual(report["continuity"]["target_write_block_before"], "off")

    def test_every_status_read_of_the_run_uses_the_reverse_names(self):
        code, report, _, _ = self.apply_flow(lambda: as_reverse(caught_up()))
        self.assertEqual(code, 0, report)
        status_reads = [params for _, name, params in self.calls if name in ("source_status", "target_status")]
        self.assertGreater(len(status_reads), 4)
        self.assertTrue(all(params["forward"] == "t8_target_to_source" for params in status_reads))

    def test_a_pending_cleanup_names_the_reverse_in_the_way_to_finish_it(self):
        code, report, _, _ = self.apply_flow(lambda: as_reverse(caught_up()), "--probe-timeout", "0", delete_never_arrives=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["marker"]["cleanup"], "pending")
        self.assertEqual(report["marker"]["pending_on"], ["source"])
        self.assertTrue(report["marker"]["resume"].startswith("resume_replication.py --direction reverse --cleanup-marker "))

    def test_the_marker_is_written_only_to_the_target_through_its_own_probe_dsn(self):
        code, report, _, written = self.apply_flow(lambda: as_reverse(caught_up()))
        self.assertEqual(code, 0, report)
        self.assertEqual({host for host, _ in written}, {"target-probe.invalid"})
        self.assertEqual({identity["system_identifier"] for _, identity in written}, {TARGET_ID})

    def test_a_second_writer_appearing_during_the_catch_up_stops_the_run_before_the_marker(self):
        def both_writing():
            state = as_reverse(caught_up())
            state["target"]["target_app_login"] = True  # The source app can log in again.
            return state

        code, report, events, written = self.apply_flow(both_writing)
        self.assertEqual(code, 1)
        self.assertEqual(report["catch_up"]["reason"], "failed_check")
        self.assertFalse(report["catch_up"]["checks"]["single_writer_kept"])
        self.assertEqual(written, [])
        self.assertEqual(events, [("enable", "source.invalid")])


class RelabelTests(unittest.TestCase):
    def test_names_are_swapped_and_values_are_not(self):
        report = {"refusals": ["reverse_active", "missing_T8_RESUME_TARGET_DSN"], "pending_on": ["target"],
                  "would_run": 'ALTER SUBSCRIPTION "t8_target_to_source" ENABLE;',
                  "preflight": {"source": {"source_slot_active": True, "database": "chat"}}, "barrier_gaps": ["barrier_not_published"]}
        self.assertEqual(resume.relabel(report), {
            "refusals": ["forward_active", "missing_T8_RESUME_TARGET_DSN"], "pending_on": ["source"],
            "would_run": 'ALTER SUBSCRIPTION "t8_target_to_source" ENABLE;',
            "preflight": {"target": {"target_slot_active": True, "database": "chat"}}, "barrier_gaps": ["barrier_not_published"]})

    def test_a_pending_cleanup_says_how_to_finish_it_in_the_same_direction(self):
        report = resume.new_marker_report("00000000-0000-4000-8000-000000000001")
        resume.remove_marker(lambda *_: resume.MarkerWrite("0/1", "0/2"), lambda: {"barrier_id": "x"}, lambda: None, report,
                             lambda condition: {"elapsed_s": 0} if condition() else None,
                             command="resume_replication.py --direction reverse")
        self.assertEqual(report["cleanup"], "pending")
        self.assertTrue(report["resume"].startswith("resume_replication.py --direction reverse --cleanup-marker "))


if __name__ == "__main__":
    unittest.main()
