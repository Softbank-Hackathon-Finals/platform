"""The subscription metrics rendered for CNPG, and the helpers that read them back in the labs."""
import importlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "infra/envs/target-workloads"
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(TOOL_DIR / "testing"))
render = importlib.import_module("render")
metrics = importlib.import_module("subscription_metrics")


class MetricsRenderTests(unittest.TestCase):
    def setUp(self):
        self.objects = render.database("node-a")
        self.cluster = next(item for item in self.objects if item["kind"] == "Cluster")
        self.config = next(item for item in self.objects if item["kind"] == "ConfigMap")

    def queries(self):
        reference = self.cluster["spec"]["monitoring"]["customQueriesConfigMap"][0]
        self.assertEqual(reference["name"], self.config["metadata"]["name"])
        # JSON is a subset of YAML, which is what CNPG parses this key as.
        return json.loads(self.config["data"][reference["key"]])

    def test_the_cluster_names_a_configmap_that_exists_before_it_in_the_same_namespace(self):
        kinds = [item["kind"] for item in self.objects]
        self.assertLess(kinds.index("ConfigMap"), kinds.index("Cluster"))
        self.assertEqual(self.config["metadata"]["namespace"], self.cluster["metadata"]["namespace"])
        self.assertEqual(self.config["metadata"]["labels"]["component"], "target")
        self.queries()

    def test_instances_reload_the_queries_without_a_manual_step(self):
        self.assertEqual(self.config["metadata"]["labels"]["cnpg.io/reload"], "true")

    def test_the_metric_list_names_the_three_signals_with_the_right_types(self):
        query = self.queries()["pg_subscription"]
        columns = {name: mapping["usage"] for item in query["metrics"] for name, mapping in item.items()}
        self.assertEqual(columns, {"subname": "LABEL", "enabled": "GAUGE", "apply_error_count": "COUNTER", "apply_workers": "GAUGE"})

    def test_a_replica_does_not_report_a_stopped_worker(self):
        self.assertTrue(self.queries()["pg_subscription"]["primary"])

    def test_the_connection_string_column_is_never_read(self):
        self.assertNotIn("subconninfo", self.queries()["pg_subscription"]["query"])

    def test_only_the_monitoring_block_and_its_configmap_are_new(self):
        self.assertEqual([item["kind"] for item in self.objects], ["Namespace", "StorageClass", "PersistentVolume", "ConfigMap", "Cluster"])
        self.assertEqual(set(self.cluster["spec"]), {"instances", "imageName", "enableSuperuserAccess", "storage", "certificates",
                                                     "resources", "postgresql", "bootstrap", "managed", "monitoring"})
        self.assertEqual(self.cluster["spec"]["postgresql"]["parameters"], {
            "wal_level": "logical", "password_encryption": "scram-sha-256", "log_connections": "on", "log_disconnections": "on",
            "pgaudit.log": "ddl,role", "pgaudit.log_parameter": "off", "log_statement": "none"})


class ExporterPageTests(unittest.TestCase):
    # The shape the real exporter prints (see the CNPG run in the evidence); other metrics surround it.
    PAGE = """# HELP cnpg_pg_subscription_enabled 1 if the subscription is enabled
# TYPE cnpg_pg_subscription_enabled gauge
cnpg_pg_subscription_enabled{subname="t8_source_to_target"} 1
# TYPE cnpg_pg_subscription_apply_error_count counter
cnpg_pg_subscription_apply_error_count{subname="t8_source_to_target"} 3
cnpg_pg_replication_slots_active{slot_name="t8_target_to_source",slot_type="logical",database="chat"} 0
cnpg_collector_up{cluster="target-db"} 1
"""

    def test_only_subscription_samples_and_their_types_are_kept(self):
        page = metrics.parse_exposition(self.PAGE)
        self.assertEqual(page["samples"], {"t8_source_to_target": {"enabled": 1.0, "apply_error_count": 3.0}})
        self.assertEqual(page["types"], {"cnpg_pg_subscription_enabled": "gauge", "cnpg_pg_subscription_apply_error_count": "counter"})


class ScrapeTests(unittest.TestCase):
    QUERY = render.SUBSCRIPTION_QUERIES["pg_subscription"]

    def test_a_row_becomes_values_keyed_by_the_label(self):
        scraped = metrics.run_query(lambda _: (["subname", "enabled", "apply_error_count", "apply_workers"], [("a", 1, 3, 0)]), self.QUERY)
        self.assertEqual(scraped, {"a": {"enabled": 1.0, "apply_error_count": 3.0, "apply_workers": 0.0}})

    def test_columns_that_differ_from_the_configuration_are_refused_as_cnpg_would_drop_them(self):
        with self.assertRaises(ValueError):
            metrics.run_query(lambda _: (["subname", "enabled"], [("a", 1)]), self.QUERY)

    def test_no_subscription_gives_no_series_rather_than_a_zero(self):
        self.assertEqual(metrics.run_query(lambda _: (["subname", "enabled", "apply_error_count", "apply_workers"], []), self.QUERY), {})


class CondenseTests(unittest.TestCase):
    @staticmethod
    def trial(outcome, *, on=True, failure="cut_8s", flowing=None, errors=1, enabled=0.0, workers=0.0):
        return {"disable_on_error": on, "failure": failure, "outcome": outcome, "rows_flowing_after_heal_s": flowing,
                "apply_errors_added": errors, "enabled_lowest": enabled, "workers_lowest": workers}

    def test_each_setting_and_failure_gets_one_counted_row(self):
        rows = metrics.condense([self.trial("stopped"), self.trial("stopped"), self.trial("recovered", on=False, flowing=2.0, enabled=1.0),
                                 self.trial("recovered", on=False, flowing=4.0, errors=3, enabled=1.0, workers=1.0)])
        self.assertEqual([(row["disable_on_error"], row["runs"], row["stopped"], row["recovered"], row["neither"]) for row in rows],
                         [(True, 2, 2, 0, 0), (False, 2, 0, 2, 0)])
        self.assertEqual(rows[1]["rows_flowing_after_heal_s"], {"median": 3.0, "max": 4.0})
        self.assertEqual(rows[1]["apply_errors_added"], {"min": 1, "median": 2.0, "max": 3})
        self.assertEqual(rows[1]["workers_lowest"], [0.0, 1.0])
        self.assertIsNone(rows[0]["rows_flowing_after_heal_s"])

    def test_a_run_that_neither_stopped_nor_recovered_stays_visible(self):
        self.assertEqual(metrics.condense([self.trial("no_recovery_seen")])[0]["neither"], 1)


if __name__ == "__main__":
    unittest.main()
