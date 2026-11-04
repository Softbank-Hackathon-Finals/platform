"""Actual v0.10.2 output projections, not a cluster security qualification."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bravo_controls.tool_reports import CheckMapping, ToolRun, assess_kube_bench

FIXTURES = Path(__file__).parent / "fixtures" / "kube-bench"


@pytest.fixture(params=["rke2-cis-1.7", "eks-1.5.0"])
def captured(request):
    benchmark = request.param
    output = (FIXTURES / f"{benchmark}.json").read_bytes()
    checks = json.loads(output)["Controls"][0]["tests"][0]["results"]
    run = ToolRun("kube-bench", "0.10.2", "offline-output-only", "fixture", output, 0, True)
    # Deliberately test a candidate mapping. This is NOT a deployable IAM mapping.
    mapping = CheckMapping(
        "kube-bench", "0.10.2", run.scope, "test-only",
        tuple((check["test_number"], run.target) for check in checks), benchmark,
    )
    return run, mapping


def test_successful_process_does_not_mean_automated_security_pass(captured):
    run, mapping = captured
    result = assess_kube_bench((run,), mapping)
    assert run.exit_code == 0
    assert result.observed is None
    assert result.unknown_reason == "UNEXPECTED_RESPONSE"


def test_unqualified_mapping_remains_not_run(captured):
    run, _ = captured
    result = assess_kube_bench((run,), None)
    assert result.observed is None
    assert result.unknown_reason == "NOT_RUN"


def test_manual_marker_cannot_be_replaced_by_summary_or_title(captured):
    run, mapping = captured
    document = json.loads(run.output)
    document["Totals"] = {"total_pass": 6, "total_fail": 0, "total_warn": 0}
    for check in document["Controls"][0]["tests"][0]["results"]:
        check.update(status="PASS", test_desc="Automated", reason="")
    result = assess_kube_bench((replace(run, output=json.dumps(document).encode()),), mapping)
    assert result.observed is None


def test_provider_specific_benchmark_is_not_interchangeable(captured):
    run, mapping = captured
    result = assess_kube_bench((run,), replace(mapping, benchmark_version="cis-1.10"))
    assert result.observed is None
    assert result.unknown_reason == "TOOL_ERROR"


def test_incomplete_cluster_collection_cannot_be_passed_as_output_qualification(captured):
    run, mapping = captured
    result = assess_kube_bench((replace(run, complete=False),), mapping)
    assert result.observed is None
    assert result.unknown_reason == "TOOL_ERROR"
