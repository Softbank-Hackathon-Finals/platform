"""Metric arithmetic, read-only guard and shaping rules for the T8 replication-lag tool."""

import importlib
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/envs/target-workloads/testing"))
metrics = importlib.import_module("lag_metrics")
readonly = importlib.import_module("lag_readonly")
lab = importlib.import_module("lag_lab")


class MetricTests(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(metrics.percentile(values, 95), 95)
        self.assertEqual(metrics.percentile(values, 100), 100)
        self.assertEqual(metrics.percentile([7], 50), 7)

    def test_summary_reports_tail_and_refuses_to_invent_values(self):
        summary = metrics.summarize([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(summary, {"n": 5, "mean": 22.0, "p50": 3.0, "p95": 100.0, "p99": 100.0, "max": 100.0})
        self.assertEqual(metrics.summarize([]), {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None})

    def test_parse_lsn_handles_both_halves(self):
        self.assertEqual(metrics.parse_lsn("0/10"), 16)
        self.assertEqual(metrics.parse_lsn("1/0"), 2 ** 32)
        self.assertIsNone(metrics.parse_lsn(None))

    def test_row_lags_use_first_sample_that_includes_the_row(self):
        acks = [(1, 10.000), (2, 10.020), (3, 10.040)]
        seen = [(10.005, None), (10.010, 0), (10.015, 1), (10.050, 3)]
        lags, missing = metrics.row_lags(acks, seen)
        self.assertEqual([round(lag, 3) for lag in lags], [15.0, 30.0, 10.0])
        self.assertEqual(missing, 0)

    def test_row_lags_count_rows_that_never_arrive(self):
        lags, missing = metrics.row_lags([(1, 1.0), (2, 2.0)], [(1.5, 1)])
        self.assertEqual([round(lag) for lag in lags], [500])
        self.assertEqual(missing, 1)

    def test_first_reach_returns_none_when_position_never_arrives(self):
        samples = [(1.0, 5), (2.0, 10)]
        self.assertEqual(metrics.first_reach(samples, 10), 2.0)
        self.assertEqual(metrics.first_reach(samples, 6), 2.0)
        self.assertIsNone(metrics.first_reach(samples, 11))
        self.assertEqual(metrics.first_reach([(1.0, None), (2.0, 3)], 1), 2.0)

    def test_position_lags_only_count_arrivals_after_the_source_sample(self):
        source = [(1.0, 100), (2.0, 200), (3.0, 300)]
        target = [(0.5, 300), (1.2, 100), (2.5, 250), (3.0, 250)]
        # The first target row precedes every source sample in time, so it must not satisfy them.
        lags = metrics.position_lags(source, target)
        self.assertEqual([round(lag) for lag in lags], [200, 500])

    def test_condense_keeps_raw_values_with_median_and_max(self):
        self.assertEqual(metrics.condense([5.0, 1.0, 3.0]), {"raw": [5.0, 1.0, 3.0], "median": 3.0, "max": 5.0})
        self.assertEqual(metrics.condense([None, 2.0]), {"raw": [None, 2.0], "median": None, "max": None, "unreached": 1})

    def test_budget_uses_worst_values_and_never_hides_an_overrun(self):
        result = metrics.budget(5000, catchup_ms=40, compare_ms=1300)
        self.assertEqual(result, {"budget_ms": 5000, "catchup_ms": 40, "compare_ms": 1300, "remaining_for_other_steps_ms": 3660})
        self.assertEqual(metrics.budget(1000, 40, 1300)["remaining_for_other_steps_ms"], -340)

    def test_budget_scenarios_add_executor_polling_only_to_barrier_waits(self):
        def entry(lag_max, confirmed_max):
            return {"summary": {"steady_visible_lag_max_ms": {"max": lag_max}, "catchup_confirmed_lsn_ms": {"max": confirmed_max}}}

        conditions = {"a": entry(100.0, 400.0), "b": entry(120.0, 450.0)}
        gate = {"visible_after_enable_ms": 600.0, "catchup_from_write_stop_ms": {"visible_ms": 2600.0, "confirmed_lsn_ms": 3100.0}}
        result = metrics.budget_scenarios(conditions, gate, budget_ms=5000, compare_ms=1300, poll_ms=50)
        self.assertEqual(result["barrier_worst_row"]["catchup_ms"], 170.0)
        self.assertEqual(result["confirmed_lsn_equal"]["catchup_ms"], 450.0)
        self.assertEqual(result["barrier_at_gate_limit"]["catchup_ms"], 650.0)
        # The deliberate idle time before re-enabling is excluded: confirmed follows visible by 500 ms.
        self.assertEqual(result["confirmed_at_gate_limit"]["catchup_ms"], 1100.0)
        self.assertEqual(result["confirmed_at_gate_limit"]["remaining_for_other_steps_ms"], 2600.0)

    def test_budget_scenarios_omit_what_was_not_measured(self):
        result = metrics.budget_scenarios({}, None, budget_ms=5000, compare_ms=1300, poll_ms=50)
        self.assertEqual(result, {"barrier_worst_row": None, "confirmed_lsn_equal": None,
                                  "barrier_at_gate_limit": None, "confirmed_at_gate_limit": None})

    def test_observation_reports_idle_source_instead_of_a_zero_lag(self):
        # WAL moves without any business row (a checkpoint, say): that alone is not a write to measure.
        source = [{"t": 1.0, "max_id": 5, "wal": 100, "confirmed": 100}, {"t": 2.0, "max_id": 5, "wal": 900, "confirmed": 100}]
        target = [{"t": 1.5, "max_id": 5, "received": 100, "reported": 100}]
        result = metrics.analyze_observation(source, target)
        self.assertTrue(result["source_idle"])
        self.assertEqual(result["row_lag_ms"]["n"], 0)

    def test_observation_measures_rows_and_positions_on_one_clock(self):
        source = [{"t": 1.0, "max_id": 5, "wal": 100, "confirmed": 90}, {"t": 2.0, "max_id": 6, "wal": 200, "confirmed": 100}]
        target = [{"t": 1.5, "max_id": 5, "received": 100, "reported": 90}, {"t": 2.25, "max_id": 6, "received": 200, "reported": 200}]
        result = metrics.analyze_observation(source, target)
        self.assertFalse(result["source_idle"])
        self.assertEqual(result["row_lag_ms"]["max"], 250.0)
        self.assertEqual(result["received_position_lag_ms"]["max"], 500.0)
        self.assertEqual(result["confirmed_lag_bytes"]["max"], 100)

    def test_intervals_are_gaps_between_consecutive_times(self):
        self.assertEqual([round(gap) for gap in metrics.intervals_ms([1.0, 1.5, 1.75])], [500, 250])
        self.assertEqual(metrics.intervals_ms([1.0]), [])

    def test_gate_lag_pairs_each_target_read_with_the_next_source_read(self):
        source = [{"t": 1.0, "wal": 100}, {"t": 2.0, "wal": 200}]
        target = [{"t": 0.5, "reported": 90}, {"t": 1.5, "reported": None}, {"t": 1.6, "reported": 150}, {"t": 3.0, "reported": 200}]
        self.assertEqual(metrics.gate_lag_bytes(source, target), [10, 50])


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    """Records every statement; only what the guard actually sends reaches `statements`."""

    def __init__(self, setting="on"):
        self.setting = setting
        self.statements = []
        self.read_only = None
        self.autocommit = None
        self.closed = False
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if statement == readonly.READ_ONLY_CHECK:
            return FakeCursor((self.setting,))
        return FakeCursor((1,))

    def rollback(self):
        self.rollbacks += 1

    def pipeline(self):
        return nullcontext()

    def close(self):
        self.closed = True


class ReadOnlyGuardTests(unittest.TestCase):
    def test_every_allowed_statement_is_a_plain_select(self):
        for name, statement in readonly.STATEMENTS.items():
            text = statement.strip().upper()
            self.assertTrue(text.startswith("SELECT"), name)
            for verb in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE", "CREATE", "GRANT", "COPY", "SET "):
                self.assertNotIn(verb, text.replace("OFFSET ", ""), name)

    def test_unlisted_statement_never_reaches_the_server(self):
        connection = FakeConnection()
        session = readonly.ReadOnlySession(connection)
        before = list(connection.statements)
        with self.assertRaises(KeyError):
            session.query("INSERT INTO messages DEFAULT VALUES")
        self.assertEqual(connection.statements, before)

    def test_session_refuses_a_server_that_is_not_read_only(self):
        connection = FakeConnection(setting="off")
        with self.assertRaisesRegex(readonly.NotReadOnly, "read-only"):
            readonly.ReadOnlySession(connection)
        self.assertTrue(connection.closed)

    def test_session_requests_read_only_and_ends_every_transaction(self):
        connection = FakeConnection()
        session = readonly.ReadOnlySession(connection)
        self.assertIs(connection.read_only, True)
        self.assertIs(connection.autocommit, False)
        session.query("ping")
        self.assertEqual(connection.rollbacks, 2)  # The check and the query.

    def test_dsns_come_only_from_the_environment_and_are_never_echoed(self):
        with patch.dict("os.environ", {readonly.SOURCE_DSN_ENV: "host=secret-source"}, clear=True), self.assertRaises(SystemExit) as raised:
            readonly.dsns_from_environment()
        self.assertIn(readonly.TARGET_DSN_ENV, str(raised.exception))
        self.assertNotIn("secret-source", str(raised.exception))

    def test_environment_values_are_returned_unchanged(self):
        env = {readonly.SOURCE_DSN_ENV: "host=a", readonly.TARGET_DSN_ENV: "host=b"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(readonly.dsns_from_environment(), ("host=a", "host=b"))


class ShapingTests(unittest.TestCase):
    def test_conditions_match_the_requested_assumptions(self):
        self.assertIsNone(lab.CONDITIONS["unlimited"])
        self.assertEqual(lab.CONDITIONS["100mbit-rtt30"], {"mbit": 100, "rtt_ms": 30})
        self.assertEqual(lab.CONDITIONS["20mbit-rtt30"], {"mbit": 20, "rtt_ms": 30})

    def test_each_direction_carries_half_the_round_trip(self):
        script = lab.shaping_script("eth1", {"mbit": 20, "rtt_ms": 30})
        self.assertIn("ethtool -K eth1 tso off gso off", script)
        self.assertIn("tc qdisc replace dev eth1 root netem delay 15ms rate 20mbit limit 192", script)

    def test_queue_scales_with_the_rate(self):
        self.assertEqual(lab.queue_packets(100, 15), 958)
        self.assertEqual(lab.queue_packets(20, 15), 192)

    def test_interface_name_is_validated_before_it_reaches_a_shell(self):
        with self.assertRaises(ValueError):
            lab.shaping_script("eth1; rm -rf /", {"mbit": 20, "rtt_ms": 30})

    def test_copy_cannot_finish_faster_than_the_configured_rate(self):
        self.assertEqual(lab.minimum_transfer_ms(25_000_000, 20), 10_000)
        self.assertIsNone(lab.minimum_transfer_ms(25_000_000, None))

    def test_only_this_tool_prefix_is_accepted_for_removal(self):
        lab.require_owned_name("t8-lag-abc-src")
        for name in ("t13-test-pg18", "pokeclip-segment-indexer-1", "t8-lagging"):
            with self.assertRaises(ValueError):
                lab.require_owned_name(name)


if __name__ == "__main__":
    unittest.main()
