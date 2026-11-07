"""IAM-01.2 collectors: trusted tool runs + pinned server mapping → Observation.

The report normalizers live in ``tool_reports`` / ``prowler_iam``. This module only
binds an assessment to the current invocation. It never runs tools itself and never
accepts an uploaded report: ``collect_runs`` / ``assess`` are host-owned collectors.

The onprem mapping below is a *candidate* built from the captured kube-bench v0.10.2
``rke2-cis-1.7`` output (docs/evidence/t14-kube-bench-2026-11-03). Its 5.1.x checks are
``manual``/``WARN``, so the assessment is UNKNOWN (UNEXPECTED_RESPONSE), never PASS,
until T5/T14 qualify automated checks for the real target.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime

from .runner import IDENTIFIER, Observation, RunContext, TestSpec
from .tool_reports import CheckMapping, ReportAssessment, ToolRun, assess_kube_bench

SPEC = TestSpec("IAM-01.2", "IAM-01", "1", "absent", "not_applicable")
KUBE_BENCH_VERSION = "0.10.2"
RKE2_BENCHMARK = "rke2-cis-1.7"
RKE2_RBAC_CHECKS = ("5.1.1", "5.1.2", "5.1.3", "5.1.4", "5.1.5", "5.1.6")
RKE2_MAPPING_VERSION = "rke2-cis-1.7-5.1-candidate"


def rke2_rbac_mapping(scope: str, targets: tuple[str, ...]) -> CheckMapping:
    """Candidate onprem IAM-01.2 mapping: CIS RKE2 1.7 section 5.1 on every node target."""
    if not isinstance(targets, tuple) or not targets or len(set(targets)) != len(targets):
        raise ValueError("registered kube-bench targets required")
    return CheckMapping(
        "kube-bench",
        KUBE_BENCH_VERSION,
        scope,
        RKE2_MAPPING_VERSION,
        tuple((check, target) for target in targets for check in RKE2_RBAC_CHECKS),
        RKE2_BENCHMARK,
    )


class ReportProbe:
    """Bind a host-produced ``ReportAssessment`` to this run (aws: Prowler IAM)."""

    def __init__(
        self,
        *,
        assess: Callable[[RunContext], ReportAssessment],
        tool: str,
        tool_version: str,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not all(callable(x) for x in (assess, evidence_sink, clock))
            or not isinstance(tool, str)
            or not IDENTIFIER.fullmatch(tool)
            or not isinstance(tool_version, str)
            or not IDENTIFIER.fullmatch(tool_version)
        ):
            raise ValueError("trusted report collector required")
        self.assess, self.tool, self.tool_version = assess, tool, tool_version
        self.sink, self.clock = evidence_sink, clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if not isinstance(context, RunContext) or spec != SPEC:
            raise ValueError("IAM-01.2 method mismatch")
        result = self.assess(context)
        if not isinstance(result, ReportAssessment):
            raise ValueError("report assessment required")
        collected = self.clock().astimezone(UTC)
        payload = json.dumps(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "tool": self.tool,
                "tool_version": self.tool_version,
                "report_sha256": result.output_sha256,
                "summary": result.summary,
                "observed": result.observed,
                "unknown_reason": result.unknown_reason,
                "collected_at": collected.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            result.observed,
            "not_applicable",
            collected,
            self.tool,
            self.tool_version,
            result.output_sha256,
            result.unknown_reason,
        )


class KubeBenchIamProbe(ReportProbe):
    """IAM-01.2 onprem: kube-bench node reports against a pinned (candidate) mapping."""

    def __init__(
        self,
        *,
        collect_runs: Callable[[RunContext], tuple[ToolRun, ...]],
        mapping: CheckMapping,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if not callable(collect_runs) or not isinstance(mapping, CheckMapping):
            raise ValueError("trusted kube-bench collector and pinned mapping required")
        super().__init__(
            assess=lambda context: assess_kube_bench(tuple(collect_runs(context)), mapping),
            tool="kube-bench",
            tool_version=mapping.version,
            evidence_sink=evidence_sink,
            clock=clock,
        )
        self.mapping = mapping
