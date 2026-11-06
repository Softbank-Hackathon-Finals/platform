"""Offline qualification of the pinned scanner, never an IAM control verdict.

Use the upstream expansion function and its complete immutable pattern set.
Do not delete unsupported patterns or reinterpret IAM policies to obtain PASS.
The caller still owns its process deadline and the ordinary live qualification.
"""

import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path

from .prowler_iam import VERSION

ANALYZER_PATH = "prowler/providers/aws/services/iam/lib/privilege_escalation.py"
ANALYZER_SHA256 = "88411a627154c1ace9e7f16e00d32b4290f1187fe00bfdf911a6800c36089fb3"
EXPAND_VERSION = "0.3.0"
MAX_PATTERNS = 2048


def _result(ready, reason, **facts):
    return {
        "kind": "prowler-analyzer-capability.v1",
        "ready": ready,
        "reason": reason,
        "full_iam_01_2_qualified": False,
        "aws_api_calls": 0,
        **facts,
    }


def _assess_patterns(combinations, expand):
    """Inspect upstream public patterns only; never accepts a role/policy input."""
    if (
        type(combinations) is not dict
        or not 1 <= len(combinations) <= MAX_PATTERNS
        or not callable(expand)
    ):
        return _result(False, "INVALID_ANALYZER_PATTERNS")
    patterns = set()
    for key, values in combinations.items():
        if (
            type(key) is not str
            or not key
            or type(values) is not set
            or not values
            or any(type(v) is not str or not 1 <= len(v) <= 256 for v in values)
        ):
            return _result(False, "INVALID_ANALYZER_PATTERNS")
        patterns.update(values)
        if len(patterns) > MAX_PATTERNS:
            return _result(False, "ANALYZER_PATTERN_LIMIT")
    pattern_digest = hashlib.sha256(
        json.dumps(sorted(patterns), separators=(",", ":")).encode()
    ).hexdigest()
    facts = {"pattern_count": len(patterns), "patterns_sha256": pattern_digest}
    for index, pattern in enumerate(sorted(patterns)):
        try:
            expanded = expand(pattern)
            if (
                type(expanded) is not set
                or not expanded
                or len(expanded) > 100_000
                or any(type(v) is not str or not 1 <= len(v) <= 256 for v in expanded)
            ):
                raise ValueError()
        except Exception:
            # Exception text may contain file paths or unrelated provider data.
            # Pattern identifiers are digests, not policies or diagnostics.
            return _result(
                False,
                "ANALYZER_CATALOGUE_INCOMPATIBLE",
                **facts,
                expanded_patterns=index,
                rejected_pattern_sha256=hashlib.sha256(pattern.encode()).hexdigest(),
            )
    return _result(True, "READY_FOR_LIVE_QUALIFICATION", **facts, expanded_patterns=len(patterns))


def probe_pinned_environment():
    """Lazy optional Prowler import, no AWS provider/session/credentials access.

    A ready result only permits attempting the existing read-only qualification.
    It is not a mapping approval, a policy assessment, or evidence of least privilege.
    The trusted host must own the installed environment and its dependency pins.
    """
    try:
        if (
            importlib.metadata.version("prowler") != VERSION
            or importlib.metadata.version("py-iam-expand") != EXPAND_VERSION
        ):
            return _result(False, "UNSUPPORTED_SCANNER_ENVIRONMENT")
        distribution = importlib.metadata.distribution("prowler")
        source = Path(distribution.locate_file(ANALYZER_PATH))
        if source.is_symlink() or source.stat().st_size > 256 * 1024:
            return _result(False, "ANALYZER_SOURCE_UNCONFIRMED")
        if hashlib.sha256(source.read_bytes()).hexdigest() != ANALYZER_SHA256:
            return _result(False, "ANALYZER_SOURCE_UNCONFIRMED")
        module = importlib.import_module(
            "prowler.providers.aws.services.iam.lib.privilege_escalation"
        )
        if Path(module.__file__).resolve() != source.resolve():
            return _result(False, "ANALYZER_SOURCE_UNCONFIRMED")
        result = _assess_patterns(
            module.privilege_escalation_policies_combination, module.expand_actions
        )
        result.update(
            scanner_version=VERSION,
            expand_version=EXPAND_VERSION,
            analyzer_sha256=ANALYZER_SHA256,
            iamdata_version=importlib.metadata.version("iamdata"),
        )
        return result
    except Exception:
        return _result(False, "SCANNER_CAPABILITY_UNAVAILABLE")
