"""Pure, fail-closed normalization of externally collected security reports.

These functions neither execute tools nor authenticate evidence. ToolRun metadata and
the mapping MUST come from a trusted collector/pinned server configuration, not the
uploaded JSON. In particular scope binds the environment, immutable input revision,
scanner configuration and exclusions. ``complete`` means that collector verified the
whole approved scope was scanned without truncation, timeout or collection errors.

No IAM-01.2 mapping is provided: real tool/version/target qualification is still due.
Shapes were checked against upstream sources. Synthetic tests are supplemented by
captured manual/WARN output projections; those are not live cluster qualifications.
See docs/evidence/t14-kube-bench-2026-11-03 in the repository root.
https://github.com/prowler-cloud/prowler/blob/5.10.0/prowler/lib/outputs/ocsf/ocsf.py
https://github.com/aquasecurity/kube-bench/blob/v0.10.2/check/controls.go
https://github.com/aquasecurity/kube-bench/blob/main/check/check.go
https://github.com/aquasecurity/kube-bench/blob/main/cmd/common.go
https://github.com/gitleaks/gitleaks/blob/v8.24.3/report/finding.go
https://github.com/gitleaks/gitleaks/blob/v8.24.3/README.md
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

MAX_REPORT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 100_000
_VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


@dataclass(frozen=True)
class ReportAssessment:
    observed: str | None
    unknown_reason: str | None
    summary: dict[str, str | int]
    output_sha256: str


@dataclass(frozen=True, repr=False)
class ToolRun:
    """Collector-owned metadata. Raw output is deliberately absent from repr."""

    tool: str
    version: str
    scope: str
    target: str
    output: bytes = field(repr=False)
    exit_code: int
    complete: bool


@dataclass(frozen=True)
class CheckMapping:
    tool: str
    version: str
    scope: str
    mapping_version: str
    # Exact (check ID, resource/node identity) pairs; never inferred from output.
    required: tuple[tuple[str, str], ...]
    benchmark_version: str | None = None


@dataclass(frozen=True)
class GitleaksMapping:
    version: str
    source_scope: str
    rendered_scope: str


class _InvalidReport(ValueError):
    """Internal exception has no untrusted details attached."""


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _version(value: object) -> bool:
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


def _digest(runs: tuple[ToolRun, ...]) -> str:
    # One report hashes its exact bytes. Multiple reports use length framing in
    # caller order; this is an integrity digest, never a signature or provenance.
    outputs = [run.output if isinstance(run.output, bytes) else b"" for run in runs]
    if len(outputs) == 1:
        return "sha256:" + hashlib.sha256(outputs[0]).hexdigest()
    digest = hashlib.sha256(b"bravo-control-reports-v1\0")
    for output in outputs:
        digest.update(len(output).to_bytes(8, "big"))
        digest.update(output)
    return "sha256:" + digest.hexdigest()


def _unknown(runs: tuple[ToolRun, ...], reason: str, detail: str) -> ReportAssessment:
    return ReportAssessment(None, reason, {"detail": detail}, _digest(runs))


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidReport()
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise _InvalidReport()


def _load(output: bytes) -> object:
    if not isinstance(output, bytes) or not output or len(output) > MAX_REPORT_BYTES:
        raise _InvalidReport()
    try:
        return json.loads(
            output.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _InvalidReport() from None


def _array(value: object) -> list:
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise _InvalidReport()
    return value


def _mapping(mapping: CheckMapping | None, tool: str) -> bool:
    if not isinstance(mapping, CheckMapping):
        return False
    if mapping.tool != tool or not _version(mapping.version):
        return False
    if not _text(mapping.scope) or not _text(mapping.mapping_version):
        return False
    required = mapping.required
    if not isinstance(required, tuple) or not 0 < len(required) <= MAX_ROWS:
        return False
    if any(
        not isinstance(pair, tuple) or len(pair) != 2 or not all(map(_text, pair))
        for pair in required
    ):
        return False
    return len(required) == len(set(required))


def _run(run: ToolRun, tool: str, version: str, scope: str, exits: tuple[int, ...]) -> bool:
    return (
        isinstance(run, ToolRun)
        and run.tool == tool
        and run.version == version
        and run.scope == scope
        and _text(run.target)
        and run.complete is True
        and type(run.exit_code) is int
        and run.exit_code in exits
    )


def _finish(
    runs: tuple[ToolRun, ...],
    required: tuple[tuple[str, str], ...],
    rows: dict[tuple[str, str], str],
) -> ReportAssessment:
    if any(key not in rows for key in required):
        return _unknown(runs, "NOT_RUN", "required_check_or_target_missing")
    selected = [rows[key] for key in required]
    if any(status not in {"PASS", "FAIL"} for status in selected):
        return _unknown(runs, "UNEXPECTED_RESPONSE", "unsupported_check_status")
    failures = selected.count("FAIL")
    return ReportAssessment(
        "present" if failures else "absent",
        None,
        {"checks": len(selected), "violations": failures},
        _digest(runs),
    )


def assess_prowler(run: ToolRun, mapping: CheckMapping | None) -> ReportAssessment:
    """Normalize Prowler OCSF JSON, not ASFF or summary counts."""
    runs = (run,)
    if not _mapping(mapping, "prowler"):
        return _unknown(runs, "NOT_RUN", "mapping_unqualified")
    assert mapping is not None
    if not _run(run, "prowler", mapping.version, mapping.scope, (0, 3)):
        return _unknown(runs, "TOOL_ERROR", "collection_binding_or_execution_invalid")
    rows = {}
    try:
        for item in _array(_load(run.output)):
            metadata = item["metadata"]
            product = metadata["product"]
            resources = _array(item["resources"])
            if (
                product["uid"] != "prowler"
                or product["version"] != mapping.version
                or len(resources) != 1
            ):
                raise _InvalidReport()
            key = (metadata["event_code"], resources[0]["uid"])
            if not all(map(_text, key)) or key in rows:
                raise _InvalidReport()
            # Muted/suppressed findings cannot stand in for an executed control.
            status = item["status_code"] if item.get("status") == "New" else "UNKNOWN"
            rows[key] = status if isinstance(status, str) else "UNKNOWN"
    except (KeyError, TypeError, IndexError, _InvalidReport):
        return _unknown(runs, "TOOL_ERROR", "malformed_or_duplicate_report")
    return _finish(runs, mapping.required, rows)


def assess_kube_bench(runs: tuple[ToolRun, ...], mapping: CheckMapping | None) -> ReportAssessment:
    """Normalize one kube-bench JSON report per collector-bound node/target."""
    if not _mapping(mapping, "kube-bench") or not _text(mapping.benchmark_version):
        return _unknown(runs, "NOT_RUN", "mapping_unqualified")
    assert mapping is not None
    if any(not _run(run, "kube-bench", mapping.version, mapping.scope, (0,)) for run in runs):
        return _unknown(runs, "TOOL_ERROR", "collection_binding_or_execution_invalid")
    expected_targets = {target for _, target in mapping.required}
    if not runs or {run.target for run in runs} != expected_targets:
        return _unknown(runs, "NOT_RUN", "required_target_missing_or_unexpected")
    if len(runs) != len(expected_targets):
        return _unknown(runs, "TOOL_ERROR", "duplicate_target_report")
    rows = {}
    try:
        for run in runs:
            # Standard invocation must leave kube-bench's default exit-code=0;
            # an execution error is not interpreted as a policy failure.
            document = _load(run.output)
            controls = _array(document["Controls"])
            for control in controls:
                if control["version"] != mapping.benchmark_version:
                    raise _InvalidReport()
                for group in _array(control["tests"]):
                    for check in _array(group["results"]):
                        key = (check["test_number"], run.target)
                        if not _text(key[0]) or key in rows or len(rows) >= MAX_ROWS:
                            raise _InvalidReport()
                        # kube-bench may use FAIL for audit command errors.
                        # A reason or manual/skip marker is not a measured violation.
                        status = check["status"]
                        if check.get("reason") or check.get("type") in {"manual", "skip"}:
                            status = "UNKNOWN"
                        rows[key] = status if isinstance(status, str) else "UNKNOWN"
    except (KeyError, TypeError, IndexError, _InvalidReport):
        return _unknown(runs, "TOOL_ERROR", "malformed_or_duplicate_report")
    return _finish(runs, mapping.required, rows)


def assess_gitleaks(
    source: ToolRun | None, rendered: ToolRun | None, mapping: GitleaksMapping | None
) -> ReportAssessment:
    """Require both pinned scans. Zero findings from only one is not SEC-01.2."""
    runs = tuple(run for run in (source, rendered) if run is not None)
    if (
        not isinstance(mapping, GitleaksMapping)
        or not _version(mapping.version)
        or not _text(mapping.source_scope)
        or not _text(mapping.rendered_scope)
        or mapping.source_scope == mapping.rendered_scope
    ):
        return _unknown(runs, "NOT_RUN", "mapping_unqualified")
    if source is None or rendered is None:
        return _unknown(runs, "NOT_RUN", "source_and_rendered_required")
    counts = []
    try:
        for run, target, scope in (
            (source, "source", mapping.source_scope),
            (rendered, "rendered", mapping.rendered_scope),
        ):
            if not _run(run, "gitleaks", mapping.version, scope, (0, 1)) or run.target != target:
                return _unknown(runs, "TOOL_ERROR", "collection_binding_or_execution_invalid")
            findings = _array(_load(run.output))
            seen = set()
            for finding in findings:
                if not all(_text(finding[name]) for name in ("RuleID", "File", "Fingerprint")):
                    raise _InvalidReport()
                if (
                    type(finding["StartLine"]) is not int
                    or type(finding["EndLine"]) is not int
                    or not 1 <= finding["StartLine"] <= finding["EndLine"]
                ):
                    raise _InvalidReport()
                fingerprint = finding["Fingerprint"]
                if fingerprint in seen:
                    raise _InvalidReport()
                seen.add(fingerprint)
            if run.exit_code != (1 if findings else 0):
                raise _InvalidReport()
            counts.append(len(findings))
    except (KeyError, TypeError, IndexError, _InvalidReport):
        return _unknown(runs, "TOOL_ERROR", "malformed_duplicate_or_exit_mismatch")
    return ReportAssessment(
        "present" if sum(counts) else "absent",
        None,
        {"source_findings": counts[0], "rendered_findings": counts[1]},
        _digest(runs),
    )
