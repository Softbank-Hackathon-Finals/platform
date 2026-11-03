import importlib.util
import unittest
from decimal import Decimal
from pathlib import Path

spec = importlib.util.spec_from_file_location("cost_report", Path(__file__).parents[1] / "cost_report.py")
cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost)


def group(project, component, amount, unit="USD"):
    return {"Keys": ["project$"+project, "component$"+component],
            "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def day(*groups):
    return {"TimePeriod": {"Start": "2026-11-01", "End": "2026-11-02"},
            "Estimated": True, "Groups": list(groups)}


class Costs(unittest.TestCase):
    def aggregate(self, batches):
        return cost.aggregate(batches, {"2026-11-01"}, Decimal(1300))

    def report(self, *groups):
        def call(service, operation, **params):
            if service == "sts":
                return {"Account": "123"}
            if operation == "list-cost-allocation-tags":
                return {"CostAllocationTags": [
                    {"TagKey": key, "Status": "Active"} for key in ("project", "component")
                ]}
            return {"ResultsByTime": [day(*groups)]}
        return cost.report("123", "2026-11-01", "2026-11-02", {"krw_per_usd": 1300}, call)

    def test_offsetting_unattributed_costs_are_not_complete(self):
        result = self.report(group("", "source", "10"), group("", "platform", "-10"))
        self.assertFalse(result["attribution_complete"])
        self.assertEqual(result["project_unattributed"]["usd"], "0")
        self.assertEqual(result["project_unattributed"]["group_count"], 2)
        self.assertTrue(all(value["usd"] is None for value in result["components"].values()))

    def test_offsetting_unknown_components_are_not_complete(self):
        result = self.report(group("bravo-platform", "", "10"),
                             group("bravo-platform", "network", "-10"))
        self.assertFalse(result["attribution_complete"])
        self.assertEqual(result["bravo_unknown_component"]["usd"], "0")
        self.assertEqual(result["bravo_unknown_component"]["group_count"], 2)
        self.assertEqual(result["account_total"]["usd"], "0")

    def test_zero_amount_unattributed_groups_are_not_complete(self):
        for project, component, field in (
            ("", "source", "project_unattributed"),
            ("bravo-platform", "", "bravo_unknown_component"),
            ("bravo-platform", "network", "bravo_unknown_component"),
        ):
            with self.subTest(project=project, component=component):
                result = self.report(group(project, component, "0"))
                self.assertFalse(result["attribution_complete"])
                self.assertEqual(result[field]["usd"], "0")
                self.assertEqual(result[field]["group_count"], 1)

    def test_fully_attributed_zero_net_cost_is_complete(self):
        result = self.report(group("bravo-platform", "source", "10"),
                             group("bravo-platform", "platform", "-10"))
        self.assertTrue(result["attribution_complete"])
        self.assertEqual(result["account_total"]["usd"], "0")
        self.assertEqual(result["project_unattributed"]["group_count"], 0)
        self.assertEqual(result["bravo_unknown_component"]["group_count"], 0)

    def test_pages_reconcile_negative_credit_and_missing_components(self):
        result = self.aggregate([[day(group("bravo-platform", "source", "3.1"), group("", "", "2"))],
                                 [day(group("bravo-platform", "platform", "-0.1"),
                                      group("other", "source", "4"), group("bravo-platform", "network", "1"))]])
        self.assertEqual(result["account_total"]["usd"], "10.0")
        self.assertEqual(result["bravo_attributed_total"]["usd"], "4.0")
        self.assertEqual(result["components"]["platform"]["usd"], "-0.1")
        self.assertIsNone(result["components"]["target"]["usd"])
        self.assertTrue(result["estimated"])

    def test_account_component_is_visible_without_project_attribution(self):
        result = self.aggregate([[day(group("", "source", "2.5"))]])
        self.assertEqual(result["account_components"]["source"]["usd"], "2.5")
        self.assertIsNone(result["components"]["source"]["usd"])
        self.assertEqual(result["project_unattributed"]["usd"], "2.5")
        self.assertEqual(result["account_total"]["usd"], "2.5")

    def test_observed_zero_differs_from_missing(self):
        result = self.aggregate([[day(group("bravo-platform", "source", "0"))]])
        self.assertEqual(result["components"]["source"]["usd"], "0")
        self.assertIsNone(result["components"]["platform"]["usd"])

    def test_reject_duplicate_missing_or_wrong_currency(self):
        for batches in ([], [[day(group("", "", "1", "KRW"))]],
                        [[day(group("", "", "1"))], [day(group("", "", "1"))]]):
            with self.assertRaises(ValueError):
                self.aggregate(batches)

    def test_pagination_and_cycle(self):
        calls = []
        def call(*args, **params):
            calls.append(params.copy())
            return {"rows": [len(calls)], **({"NextPageToken": "next"} if len(calls) == 1 else {})}
        self.assertEqual(list(cost.pages(call, "op", "rows")), [[1], [2]])
        self.assertEqual(calls[1]["NextPageToken"], "next")
        with self.assertRaises(ValueError):
            list(cost.pages(lambda *a, **k: {"rows": [], "NextPageToken": "same"}, "op", "rows"))

    def test_account_guard_and_malformed_response(self):
        with self.assertRaisesRegex(ValueError, "Wrong AWS account"):
            cost.report("expected", "2026-11-01", "2026-11-02", {}, lambda *a, **k: {"Account": "other"})
        with self.assertRaises(ValueError):
            list(cost.pages(lambda *a, **k: {}, "op", "rows"))

    def test_inactive_tags_keep_attribution_incomplete(self):
        def call(service, operation, **params):
            if service == "sts":
                return {"Account": "123"}
            if operation == "list-cost-allocation-tags":
                return {"CostAllocationTags": [{"TagKey": "component", "Status": "Inactive"}]}
            return {"ResultsByTime": [day(group("", "", "7"))]}
        result = cost.report("123", "2026-11-01", "2026-11-02", {"krw_per_usd": 1300}, call)
        self.assertFalse(result["attribution_complete"])
        self.assertEqual(result["tag_status"]["project"], "NOT_AVAILABLE")
        self.assertTrue(all(v["usd"] is None for v in result["components"].values()))

    def test_reversed_dates(self):
        with self.assertRaises(ValueError):
            cost.date_range("2026-11-03", "2026-11-02")
