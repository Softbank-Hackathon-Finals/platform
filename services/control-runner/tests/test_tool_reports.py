"""Synthetic upstream-shaped reports; these tests are NOT actual scanner evidence."""

import hashlib
import json
import unittest
from dataclasses import asdict, replace

from bravo_controls.tool_reports import (
    MAX_REPORT_BYTES,
    CheckMapping,
    GitleaksMapping,
    ToolRun,
    assess_gitleaks,
    assess_kube_bench,
    assess_prowler,
)


def raw(value):
    return json.dumps(value).encode()


def prowler_row(check="synthetic-check", target="resource-a", status="PASS"):
    return {
        "metadata": {"event_code": check, "product": {"uid": "prowler", "version": "5.10.0"}},
        "resources": [{"uid": target}],
        "status": "New",
        "status_code": status,
    }


def prowler_run(rows=None):
    return ToolRun(
        "prowler",
        "5.10.0",
        "aws-synthetic-revision-config",
        "account-a",
        raw([prowler_row()] if rows is None else rows),
        0,
        True,
    )


PROWLER = CheckMapping(
    "prowler",
    "5.10.0",
    "aws-synthetic-revision-config",
    "synthetic-1",
    (("synthetic-check", "resource-a"),),
)
KUBE = CheckMapping(
    "kube-bench",
    "0.10.2",
    "k8s-synthetic-revision-config",
    "synthetic-1",
    (("5.1.synthetic", "node-a"),),
    "1.8",
)
LEAKS = GitleaksMapping("8.24.3", "source-revision-config", "rendered-revision-config")


def kube_run(status="PASS", target="node-a", checks=None):
    checks = checks if checks is not None else [{"test_number": "5.1.synthetic", "status": status}]
    return ToolRun(
        "kube-bench",
        "0.10.2",
        KUBE.scope,
        target,
        raw(
            {
                "Controls": [{"version": "1.8", "tests": [{"results": checks}]}],
                "Totals": {"total_fail": 0},
            }
        ),
        0,
        True,
    )


def leaks_run(target="source", findings=None):
    findings = [] if findings is None else findings
    scope = LEAKS.source_scope if target == "source" else LEAKS.rendered_scope
    return ToolRun("gitleaks", "8.24.3", scope, target, raw(findings), int(bool(findings)), True)


def finding():
    return {
        "RuleID": "synthetic-rule",
        "File": "config.txt",
        "StartLine": 1,
        "EndLine": 1,
        "Fingerprint": "config.txt:synthetic-rule:1",
        "Secret": "SYNTHETIC_DO_NOT_ECHO",
        "Match": "secret=SYNTHETIC_DO_NOT_ECHO",
    }


class ProwlerReports(unittest.TestCase):
    def test_complete_check_target_set_yields_observation(self):
        run = prowler_run()
        result = assess_prowler(run, PROWLER)
        self.assertEqual(result.observed, "absent")
        self.assertEqual(result.output_sha256, "sha256:" + hashlib.sha256(run.output).hexdigest())
        self.assertEqual(result.summary, {"checks": 1, "violations": 0})
        failed = replace(prowler_run([prowler_row(status="FAIL")]), exit_code=3)
        self.assertEqual(assess_prowler(failed, PROWLER).observed, "present")

    def test_mapping_is_never_inferred_from_report(self):
        for mapping in (
            None,
            replace(PROWLER, required=()),
            replace(PROWLER, version="latest"),
            replace(PROWLER, mapping_version=""),
            replace(PROWLER, scope=""),
            replace(PROWLER, required=PROWLER.required * 2),
        ):
            with self.subTest(mapping=mapping):
                result = assess_prowler(prowler_run(), mapping)
                self.assertIsNone(result.observed)
                self.assertEqual(result.unknown_reason, "NOT_RUN")

    def test_missing_check_wrong_target_and_empty_report_are_not_zero_violations(self):
        for rows in ([], [prowler_row(check="other")], [prowler_row(target="other")]):
            with self.subTest(rows=rows):
                self.assertEqual(
                    assess_prowler(prowler_run(rows), PROWLER).unknown_reason, "NOT_RUN"
                )

    def test_extra_unmapped_check_does_not_override_selected_failure(self):
        run = prowler_run([prowler_row(status="FAIL"), prowler_row(check="other", status="PASS")])
        self.assertEqual(assess_prowler(run, PROWLER).observed, "present")

    def test_duplicate_malformed_and_wrong_embedded_version_are_unknown(self):
        version = prowler_row()
        version["metadata"]["product"]["version"] = "5.11.0"
        for rows in ([prowler_row(), prowler_row()], [{}], [version], ["PASS"]):
            with self.subTest(rows=rows):
                self.assertEqual(
                    assess_prowler(prowler_run(rows), PROWLER).unknown_reason, "TOOL_ERROR"
                )

    def test_unsupported_status_and_suppression_are_unknown(self):
        for status in ("ERROR", "MANUAL", "WARN", "pass", None, {"nested": "PASS"}):
            with self.subTest(status=status):
                result = assess_prowler(prowler_run([prowler_row(status=status)]), PROWLER)
                self.assertEqual(result.unknown_reason, "UNEXPECTED_RESPONSE")
        row = prowler_row()
        row["status"] = "Suppressed"
        self.assertIsNone(assess_prowler(prowler_run([row]), PROWLER).observed)

    def test_collector_binding_and_completion_are_required(self):
        for changes in (
            {"tool": "other"},
            {"version": "5.10.1"},
            {"scope": "other"},
            {"complete": False},
            {"exit_code": 1},
            {"exit_code": False},
        ):
            with self.subTest(changes=changes):
                result = assess_prowler(replace(prowler_run(), **changes), PROWLER)
                self.assertEqual(result.unknown_reason, "TOOL_ERROR")

    def test_invalid_json_never_leaks_raw_output(self):
        marker = "SYNTHETIC_DO_NOT_ECHO"
        for output in (
            b"",
            marker.encode(),
            b'[{"metadata":1,"metadata":2}]',
            b"[NaN]",
            b"\xff",
            b"x" * (MAX_REPORT_BYTES + 1),
            b"[" * 1500 + b"]" * 1500,
        ):
            with self.subTest(size=len(output)):
                run = replace(prowler_run(), output=output)
                result = assess_prowler(run, PROWLER)
                self.assertEqual(result.unknown_reason, "TOOL_ERROR")
                self.assertNotIn(marker, repr(result))
                self.assertNotIn(marker, repr(run))


class KubeBenchReports(unittest.TestCase):
    def test_uses_detailed_results_not_summary(self):
        self.assertEqual(assess_kube_bench((kube_run(),), KUBE).observed, "absent")
        # Totals is deliberately inconsistent: detailed FAIL still wins.
        self.assertEqual(assess_kube_bench((kube_run("FAIL"),), KUBE).observed, "present")

    def test_every_pinned_target_must_have_one_complete_report(self):
        mapping = replace(KUBE, required=KUBE.required + (("5.1.synthetic", "node-b"),))
        self.assertEqual(assess_kube_bench((kube_run(),), mapping).unknown_reason, "NOT_RUN")
        self.assertEqual(
            assess_kube_bench((kube_run(), kube_run(target="node-b")), mapping).observed, "absent"
        )
        self.assertEqual(
            assess_kube_bench((kube_run(), kube_run()), KUBE).unknown_reason, "TOOL_ERROR"
        )
        self.assertEqual(
            assess_kube_bench((kube_run(target="wrong"),), KUBE).unknown_reason, "NOT_RUN"
        )

    def test_manual_skipped_and_execution_error_are_not_measured_failures(self):
        for changes in (
            {"status": "WARN"},
            {"status": "INFO"},
            {"status": "unknown"},
            {"type": "manual"},
            {"type": "skip"},
            {"reason": "SYNTHETIC_DO_NOT_ECHO"},
        ):
            check = {"test_number": "5.1.synthetic", "status": "FAIL", **changes}
            with self.subTest(changes=changes):
                result = assess_kube_bench((kube_run(checks=[check]),), KUBE)
                self.assertEqual(result.unknown_reason, "UNEXPECTED_RESPONSE")
                self.assertNotIn("SYNTHETIC_DO_NOT_ECHO", repr(result))

    def test_empty_duplicate_missing_and_benchmark_mismatch(self):
        self.assertIsNone(
            assess_kube_bench((kube_run(),), replace(KUBE, benchmark_version="")).observed
        )
        self.assertIsNone(
            assess_kube_bench((kube_run(),), replace(KUBE, benchmark_version="1.9")).observed
        )
        self.assertIsNone(assess_kube_bench((kube_run(),), None).observed)
        for checks in (
            [],
            [{"test_number": "other", "status": "PASS"}],
            [{"test_number": "5.1.synthetic", "status": "PASS"}] * 2,
        ):
            with self.subTest(checks=checks):
                self.assertIsNone(assess_kube_bench((kube_run(checks=checks),), KUBE).observed)

    def test_collection_failures_are_unknown_before_target_comparison(self):
        for changes in (
            {"target": []},
            {"target": ""},
            {"version": "0.10.3"},
            {"scope": "other"},
            {"complete": False},
            {"exit_code": 1},
        ):
            with self.subTest(changes=changes):
                result = assess_kube_bench((replace(kube_run(), **changes),), KUBE)
                self.assertEqual(result.unknown_reason, "TOOL_ERROR")


class GitleaksReports(unittest.TestCase):
    def test_both_scopes_are_required_and_counted(self):
        self.assertEqual(
            assess_gitleaks(leaks_run(), leaks_run("rendered"), LEAKS).observed, "absent"
        )
        result = assess_gitleaks(leaks_run(findings=[finding()]), leaks_run("rendered"), LEAKS)
        self.assertEqual(result.observed, "present")
        self.assertEqual(result.summary, {"source_findings": 1, "rendered_findings": 0})
        self.assertNotIn("SYNTHETIC_DO_NOT_ECHO", json.dumps(asdict(result)))

    def test_empty_scope_and_missing_scan_cannot_pass(self):
        for source, rendered in ((None, leaks_run("rendered")), (leaks_run(), None), (None, None)):
            self.assertEqual(assess_gitleaks(source, rendered, LEAKS).unknown_reason, "NOT_RUN")
        for mapping in (
            None,
            replace(LEAKS, source_scope=""),
            replace(LEAKS, rendered_scope=LEAKS.source_scope),
            replace(LEAKS, version="8.*"),
        ):
            self.assertEqual(
                assess_gitleaks(leaks_run(), leaks_run("rendered"), mapping).unknown_reason,
                "NOT_RUN",
            )

    def test_wrong_binding_failed_exit_and_partial_execution_rejected(self):
        for changes in (
            {"scope": "wrong"},
            {"version": "8.24.4"},
            {"target": "source"},
            {"complete": False},
            {"exit_code": 2},
            {"exit_code": 1},
        ):
            with self.subTest(changes=changes):
                result = assess_gitleaks(
                    leaks_run(), replace(leaks_run("rendered"), **changes), LEAKS
                )
                self.assertIsNone(result.observed)
        bad_exit = replace(leaks_run(findings=[finding()]), exit_code=0)
        self.assertIsNone(assess_gitleaks(bad_exit, leaks_run("rendered"), LEAKS).observed)

    def test_duplicate_keys_findings_bad_shapes_do_not_escape(self):
        for findings in (
            [finding(), finding()],
            [{}],
            ["secret"],
            [{**finding(), "StartLine": True}],
            [{**finding(), "Fingerprint": ""}],
        ):
            with self.subTest(findings=findings):
                result = assess_gitleaks(leaks_run(findings=findings), leaks_run("rendered"), LEAKS)
                self.assertEqual(result.unknown_reason, "TOOL_ERROR")
                self.assertNotIn("SYNTHETIC_DO_NOT_ECHO", repr(result))


if __name__ == "__main__":
    unittest.main()
