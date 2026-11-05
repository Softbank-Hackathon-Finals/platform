"""The stall lab's link rules and its summary: pure functions, no Docker or database."""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/envs/target-workloads/testing"))
lag_lab = importlib.import_module("lag_lab")
lag_metrics = importlib.import_module("lag_metrics")


RELAY = {"mbit": 100, "rtt_ms": 90}


class StallScriptTests(unittest.TestCase):
    def test_a_stall_adds_delay_on_top_of_the_link_and_drops_nothing(self):
        self.assertEqual(lag_lab.stall_script("eth1", RELAY, extra_delay_ms=4000),
                         "tc qdisc change dev eth1 root netem delay 4045ms rate 100mbit limit 100000")

    def test_restoring_returns_to_the_plain_link_with_the_same_queue(self):
        # The queue stays deep, so every trial runs on the same rule; at 50 writes/s it never fills.
        self.assertEqual(lag_lab.stall_script("eth1", RELAY),
                         "tc qdisc change dev eth1 root netem delay 45ms rate 100mbit limit 100000")

    def test_a_cut_is_the_same_rule_losing_every_packet(self):
        self.assertEqual(lag_lab.stall_script("eth1", RELAY, loss_pct=100),
                         "tc qdisc change dev eth1 root netem delay 45ms rate 100mbit limit 100000 loss 100%")

    def test_change_not_replace_so_queued_packets_are_held_rather_than_flushed(self):
        self.assertTrue(lag_lab.stall_script("eth1", RELAY, extra_delay_ms=2000).startswith("tc qdisc change "))

    def test_only_plain_numbers_and_interface_names_reach_the_shell(self):
        for bad in ({"interface": "eth1; reboot"}, {"extra_delay_ms": -1}, {"extra_delay_ms": "4000"},
                    {"extra_delay_ms": True}, {"loss_pct": 101}, {"loss_pct": 1.5}):
            arguments = {"interface": "eth1", "shaping": RELAY, **bad}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                lag_lab.stall_script(**arguments)

    def test_without_a_shaped_link_there_is_nothing_to_change(self):
        with self.assertRaises(ValueError):
            lag_lab.stall_script("eth1", None, extra_delay_ms=2000)


def trial(timeout_s, condition, *, outcome="recovered", ended=False, errors=0, flowing=1.0, missing=0, events=()):
    return {"timeout_s": timeout_s, "condition": condition, "outcome": outcome, "connection_ended": ended,
            "apply_errors_added": errors, "rows_flowing_after_heal_s": flowing, "rows_missing": missing,
            "target_log_events": [{"event": name} for name in events], "source_log_events": []}


class StallSummaryTests(unittest.TestCase):
    def test_one_row_per_timeout_and_condition_in_the_order_first_seen(self):
        rows = lag_metrics.summarize_stall_trials([trial(5, "stall_4s"), trial(30, "stall_4s"), trial(5, "stall_4s")])
        self.assertEqual([(row["timeout_s"], row["condition"], row["runs"]) for row in rows], [(5, "stall_4s", 2), (30, "stall_4s", 1)])

    def test_ended_connections_disables_and_errors_are_counted(self):
        rows = lag_metrics.summarize_stall_trials([
            trial(5, "stall_4s", ended=True, errors=1, flowing=12.0, events=("receiver_timeout",)),
            trial(5, "stall_4s", outcome="disabled", ended=True, errors=1, flowing=None, events=("receiver_timeout", "subscription_disabled")),
            trial(5, "stall_4s", flowing=2.0)])
        row = rows[0]
        self.assertEqual((row["connection_ended"], row["disabled"], row["recovered"]), (2, 1, 2))
        self.assertEqual(row["apply_errors_added"], {"min": 0, "max": 1})
        self.assertEqual(row["rows_flowing_after_heal_s"], {"median": 7.0, "max": 12.0, "unreached": 1})
        self.assertEqual(row["timeout_events"], {"receiver_timeout": 2, "sender_timeout": 0})

    def test_a_lost_row_is_never_averaged_away(self):
        row = lag_metrics.summarize_stall_trials([trial(30, "cut_20s"), trial(30, "cut_20s", missing=3)])[0]
        self.assertEqual(row["rows_missing_max"], 3)

    def test_no_run_reaching_the_target_gives_no_time_rather_than_zero(self):
        row = lag_metrics.summarize_stall_trials([trial(5, "stall_6s", outcome="disabled", flowing=None)])[0]
        self.assertEqual(row["rows_flowing_after_heal_s"], {"median": None, "max": None, "unreached": 1})


if __name__ == "__main__":
    unittest.main()
