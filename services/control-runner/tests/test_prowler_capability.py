import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bravo_controls import prowler_capability as capability

SENTINEL = "private-provider-diagnostic-must-not-escape"


def test_all_upstream_patterns_must_expand_without_rewriting():
    seen = []

    def expand(pattern):
        seen.append(pattern)
        if pattern == "retired:Action":
            raise ValueError(SENTINEL)
        return {pattern}

    result = capability._assess_patterns(
        {"ordinary": {"a:Read"}, "escalation": {"retired:Action", "z:Write"}}, expand
    )
    assert result["ready"] is False
    assert result["reason"] == "ANALYZER_CATALOGUE_INCOMPATIBLE"
    assert result["pattern_count"] == 3 and result["expanded_patterns"] == 1
    assert seen == ["a:Read", "retired:Action"]
    assert result["rejected_pattern_sha256"] == hashlib.sha256(b"retired:Action").hexdigest()
    assert SENTINEL not in json.dumps(result)


def test_ready_is_never_an_iam_verdict_or_a_policy_read():
    patterns = {"one": {"iam:PassRole", "ec2:RunInstances"}, "two": {"iam:PassRole"}}
    result = capability._assess_patterns(patterns, lambda pattern: {pattern})
    assert result["ready"] is True
    assert result["expanded_patterns"] == result["pattern_count"] == 2
    assert result["full_iam_01_2_qualified"] is False
    assert result["aws_api_calls"] == 0
    assert patterns["two"] == {"iam:PassRole"}


@pytest.mark.parametrize("expanded", [set(), [], None, True, {1}, {"x" * 257}])
def test_empty_or_invalid_expansion_does_not_hide_a_pattern(expanded):
    result = capability._assess_patterns({"one": {"a:Read"}}, lambda _: expanded)
    assert result["ready"] is False
    assert result["reason"] == "ANALYZER_CATALOGUE_INCOMPATIBLE"


@pytest.mark.parametrize("patterns", [{}, [], {"one": set()}, {"one": ["a:Read"]},
                                      {"one": {True}}, {"": {"a:Read"}}])
def test_missing_or_malformed_complete_pattern_set_cannot_qualify(patterns):
    def forbidden(_):
        raise AssertionError("not a valid pattern set")

    assert capability._assess_patterns(patterns, forbidden)["ready"] is False


def test_pattern_limit_is_checked_before_expansion(monkeypatch):
    monkeypatch.setattr(capability, "MAX_PATTERNS", 2)
    result = capability._assess_patterns({"one": {"a:One", "a:Two", "a:Three"}}, None)
    assert result["ready"] is False
    seen = []
    result = capability._assess_patterns(
        {"one": {"a:One", "a:Two", "a:Three"}}, lambda p: seen.append(p) or {p}
    )
    assert result["reason"] == "ANALYZER_PATTERN_LIMIT" and not seen


def installed(monkeypatch, tmp_path):
    source = tmp_path / "analyzer.py"
    source.write_bytes(b"explicit synthetic source fixture")
    monkeypatch.setattr(capability, "ANALYZER_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(capability.importlib.metadata, "version", lambda name: {
        "prowler": capability.VERSION, "py-iam-expand": capability.EXPAND_VERSION,
        "iamdata": "fixture-data-version",
    }[name])
    monkeypatch.setattr(capability.importlib.metadata, "distribution",
                        lambda _: SimpleNamespace(locate_file=lambda _: source))
    calls = []
    module = SimpleNamespace(__file__=str(source),
        privilege_escalation_policies_combination={"one": {"a:Read"}},
        expand_actions=lambda p: {p})
    monkeypatch.setattr(capability.importlib, "import_module", lambda name: calls.append(name) or module)
    return source, calls, module


def test_pinned_source_and_complete_patterns_only_allow_live_qualification(monkeypatch, tmp_path):
    _, calls, _ = installed(monkeypatch, tmp_path)
    result = capability.probe_pinned_environment()
    assert result["ready"] is True and result["full_iam_01_2_qualified"] is False
    assert result["iamdata_version"] == "fixture-data-version" and len(calls) == 1


def test_modified_source_is_not_imported_to_remove_an_invalid_pattern(monkeypatch, tmp_path):
    source, calls, _ = installed(monkeypatch, tmp_path)
    source.write_text("pretend a broken upstream pattern was removed")
    result = capability.probe_pinned_environment()
    assert result["reason"] == "ANALYZER_SOURCE_UNCONFIRMED" and not calls


def test_capability_is_rechecked_instead_of_reusing_an_old_ready(monkeypatch, tmp_path):
    source, calls, _ = installed(monkeypatch, tmp_path)
    assert capability.probe_pinned_environment()["ready"] is True
    source.write_text("changed after the previous qualification")
    assert capability.probe_pinned_environment()["ready"] is False
    assert len(calls) == 1


def test_symlinked_analyzer_is_not_imported(monkeypatch, tmp_path):
    source, calls, _ = installed(monkeypatch, tmp_path)
    target = tmp_path / "actual.py"
    source.rename(target)
    source.symlink_to(target)
    assert capability.probe_pinned_environment()["reason"] == "ANALYZER_SOURCE_UNCONFIRMED"
    assert not calls


def test_imported_module_cannot_come_from_another_path(monkeypatch, tmp_path):
    _, _, module = installed(monkeypatch, tmp_path)
    module.__file__ = str(tmp_path / "different.py")
    assert capability.probe_pinned_environment()["reason"] == "ANALYZER_SOURCE_UNCONFIRMED"


def test_new_release_is_not_implicitly_accepted(monkeypatch, tmp_path):
    _, calls, _ = installed(monkeypatch, tmp_path)
    monkeypatch.setattr(capability.importlib.metadata, "version", lambda _: "5.48.0")
    assert capability.probe_pinned_environment()["reason"] == "UNSUPPORTED_SCANNER_ENVIRONMENT"
    assert not calls


def test_missing_dependency_diagnostic_does_not_escape(monkeypatch):
    def unavailable(_):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(capability.importlib.metadata, "version", unavailable)
    result = capability.probe_pinned_environment()
    assert result["ready"] is False and SENTINEL not in json.dumps(result)


@pytest.mark.parametrize("ready", [False, None, 1, "true"])
def test_incompatible_scanner_stops_before_credentials_or_registration(monkeypatch, tmp_path, ready):
    path = Path(__file__).parent / "integration/qualify_prowler_iam.py"
    spec = importlib.util.spec_from_file_location("private_iam_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "probe_pinned_environment", lambda: {"ready": ready})
    imported = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "boto3" or name.startswith("prowler."):
            raise AssertionError("AWS/scanner imports must not happen")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    module.worker("do-not-resolve-a-profile", tmp_path / "nonexistent-registration", tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_bytes())
    assert summary["observed"] is None and summary["unknown_reason"] == "TOOL_ERROR"
    assert summary["aws_api_calls"] == 0 and summary["full_iam_01_2_qualified"] is False
