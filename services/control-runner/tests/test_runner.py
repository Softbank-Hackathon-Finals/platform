import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bravo_controls import Catalog, Environment, Observation, Runner
from bravo_controls.runner import RunContext

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)
ENV = Environment("aws-source", "source", "aws:eks/source")
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def documents():
    return (
        json.loads((ROOT / "docs/controls/profiles.json").read_text()),
        json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()),
    )


@pytest.fixture
def catalog(documents):
    return Catalog(*documents)


def good(context, spec):
    return Observation(
        context,
        spec.test_id,
        spec.method_version,
        spec.expected,
        "pass" if spec.positive_control == "required" else "not_applicable",
        NOW,
        "test-collector",
        "1",
        DIGEST,
    )


def execute(catalog, probes=None, profile="demo-gate", **kwargs):
    return Runner(catalog, probes or {}, clock=lambda: NOW, **kwargs).run(
        profile_id=profile,
        profile_version="1",
        app="demo-chat",
        environment=ENV,
    )


def test_full_profile_missing_collectors_emits_all_twenty_unknown(catalog):
    run = execute(catalog, profile="full")
    specs = catalog.profile("full", "1", "demo-chat")
    assert len(run["results"]) == 20
    assert [row["test_id"] for row in run["results"]] == [spec.test_id for spec in specs]
    assert all(row["verdict"] == "UNKNOWN" for row in run["results"])
    assert all(row["unknown_reason"] == "NOT_RUN" for row in run["results"])
    assert all(row["evidence"] is None for row in run["results"])


def test_fixture_observations_form_t5_run_without_gate_authority(catalog):
    probes = {s.test_id: good for s in catalog.profile("full", "1", "demo-chat")}
    run = execute(catalog, probes, "full")
    assert all(r["verdict"] == "PASS" for r in run["results"])
    assert "decision" not in run
    assert run["environment"] == {"id": ENV.id, "role": ENV.role, "ref": ENV.ref}
    assert len({r["test_id"] for r in run["results"]}) == 20
    assert run["run_id"] != execute(catalog, probes, "full")["run_id"]


def test_registered_invocation_preserves_prepared_receipt_identity(catalog):
    context = RunContext("server-registered-backup-attempt", "demo-chat", ENV)
    seen = []

    def probe(ctx, spec):
        seen.append(ctx)
        assert ctx is context
        return good(ctx, spec)

    result = Runner(catalog, {"DR-01.1": probe}, clock=lambda: NOW).run(
        profile_id="full", profile_version="1", app="demo-chat",
        environment=ENV, registered_context=context,
    )
    assert seen == [context]
    assert result["run_id"] == context.run_id
    assert next(r for r in result["results"] if r["test_id"] == "DR-01.1")["verdict"] == "PASS"
    assert sum(r["verdict"] == "UNKNOWN" for r in result["results"]) == 19


@pytest.mark.parametrize("context", [
    {}, "claimed-id", RunContext("", "demo-chat", ENV),
    RunContext("bad\nidentifier", "demo-chat", ENV),
    RunContext("a" * 257, "demo-chat", ENV),
    RunContext("attempt", "other-app", ENV),
    RunContext("attempt", "demo-chat", replace(ENV, ref="aws:eks/other")),
    RunContext("attempt", "demo-chat", replace(ENV, role="target")),
    RunContext("attempt", "demo-chat", replace(ENV, id="other")),
])
def test_unbound_registered_invocation_is_rejected_before_probes(catalog, context):
    seen = []
    with pytest.raises(ValueError):
        Runner(catalog, {"DR-01.1": lambda *a: seen.append(a)}, clock=lambda: NOW).run(
            profile_id="full", profile_version="1", app="demo-chat", environment=ENV,
            registered_context=context,
        )
    assert not seen


def test_registered_invocation_does_not_accept_old_observation(catalog):
    context = RunContext("registered", "demo-chat", ENV)
    result = Runner(catalog, {
        "DR-01.1": lambda c, s: replace(good(c, s), collected_at=NOW - timedelta(seconds=1))
    }, clock=lambda: NOW).run(
        profile_id="full", profile_version="1", app="demo-chat", environment=ENV,
        registered_context=context,
    )
    row = next(r for r in result["results"] if r["test_id"] == "DR-01.1")
    assert row["verdict"] == "UNKNOWN" and row["evidence"] is None


@pytest.mark.parametrize(
    "observed,positive,reason,verdict,expected_reason",
    [
        ("deny", "pass", None, "PASS", None),
        ("allow", "pass", None, "FAIL", None),
        ("allow", "fail", None, "UNKNOWN", "POSITIVE_CONTROL_FAILED"),
        ("deny", "fail", None, "UNKNOWN", "POSITIVE_CONTROL_FAILED"),
        ("deny", "not_applicable", None, "UNKNOWN", "POSITIVE_CONTROL_FAILED"),
        (None, "pass", "TIMEOUT", "UNKNOWN", "TIMEOUT"),
        ("deny", "pass", "TOOL_ERROR", "UNKNOWN", "TOOL_ERROR"),
        (None, "pass", None, "UNKNOWN", "UNEXPECTED_RESPONSE"),
    ],
)
def test_verdict_table(catalog, observed, positive, reason, verdict, expected_reason):
    def probe(ctx, spec):
        return replace(
            good(ctx, spec), observed=observed, positive_control=positive, unknown_reason=reason
        )

    row = execute(catalog, {"IAM-01.1": probe})["results"][0]
    assert row["verdict"] == verdict
    assert row["unknown_reason"] == expected_reason


@pytest.mark.parametrize(
    "change",
    [
        {"test_id": "SEC-01.1"},
        {"method_version": "2"},
        {"observed": "403"},
        {"observed": {}},
        {"positive_control": "true"},
        {"unknown_reason": "EVIDENCE_EXPIRED"},
        {"collected_at": NOW + timedelta(seconds=1)},
        {"collected_at": NOW - timedelta(seconds=1)},
        {"collected_at": NOW.replace(tzinfo=None)},
        {"output_sha256": "a" * 64},
        {"output_sha256": "sha256:" + "A" * 64},
        {"tool": "collector\nsecret=value"},
        {"tool_version": ""},
    ],
)
def test_malformed_or_wrong_method_evidence_never_passes(catalog, change):
    row = execute(catalog, {"IAM-01.1": lambda c, s: replace(good(c, s), **change)})["results"][0]
    assert row["verdict"] == "UNKNOWN"
    assert row["unknown_reason"] == "TOOL_ERROR"
    assert row["evidence"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("app", "other-app"),
        ("run_id", "old-run"),
        ("environment", Environment("rke2-target", "target", "onprem:rke2/target")),
    ],
)
def test_observation_cannot_cross_app_environment_or_invocation(catalog, field, value):
    def probe(ctx, spec):
        return replace(good(ctx, spec), context=replace(ctx, **{field: value}))

    row = execute(catalog, {"IAM-01.1": probe})["results"][0]
    assert row["unknown_reason"] == "TOOL_ERROR"


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError, "TIMEOUT"),
        (PermissionError, "OBSERVATION_DENIED"),
        (RuntimeError, "TOOL_ERROR"),
    ],
)
def test_collector_exception_is_redacted_and_other_tests_continue(catalog, error, reason, caplog):
    def broken(*args):
        raise error("DO-NOT-PUBLISH-sensitive-raw-output")

    run = execute(catalog, {"IAM-01.1": broken, "SEC-01.1": good})
    assert run["results"][0]["unknown_reason"] == reason
    assert run["results"][1]["verdict"] == "PASS"
    assert "DO-NOT-PUBLISH" not in json.dumps(run) + caplog.text


def test_non_observation_result_is_not_accepted(catalog):
    run = execute(catalog, {"IAM-01.1": lambda *_: {"verdict": "PASS"}})
    assert run["results"][0]["unknown_reason"] == "TOOL_ERROR"


def test_evidence_ttl_is_bounded(catalog):
    run = execute(catalog, {"IAM-01.1": good}, evidence_ttl=timedelta(minutes=30))
    assert run["results"][0]["evidence"]["expires_at"] == "2026-11-03T09:30:00Z"
    for ttl in [timedelta(0), timedelta(seconds=-1), timedelta(hours=24, seconds=1)]:
        with pytest.raises(ValueError, match="TTL"):
            execute(catalog, evidence_ttl=ttl)


def test_ttl_does_not_expand_over_daylight_saving_transition(catalog):
    collected = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/New_York"))

    def probe(ctx, spec):
        return replace(good(ctx, spec), collected_at=collected)

    run = Runner(catalog, {"IAM-01.1": probe}, clock=lambda: collected).run(
        profile_id="demo-gate",
        profile_version="1",
        app="demo-chat",
        environment=ENV,
    )
    ev = run["results"][0]["evidence"]
    assert datetime.fromisoformat(ev["expires_at"]) - datetime.fromisoformat(
        ev["collected_at"]
    ) == timedelta(hours=24)


def test_catalog_snapshot_is_not_mutated_after_registration(documents):
    data, schema = documents
    catalog = Catalog(data, schema)
    data["tests"]["IAM-01.1"]["expected"] = "allow"
    data["profiles"][0]["required_test_ids"].clear()
    schema["$defs"].clear()
    assert len(execute(catalog)["results"]) == 3
    assert execute(catalog)["results"][0]["expected"] == "deny"


@pytest.mark.parametrize("change", ["duplicate", "unknown-test", "wrong-control"])
def test_invalid_catalog_rejected(documents, change):
    data, schema = deepcopy(documents)
    if change == "duplicate":
        data["profiles"].append(deepcopy(data["profiles"][0]))
    elif change == "unknown-test":
        data["profiles"][0]["required_test_ids"].append("SEC-01.99")
    else:
        data["tests"]["IAM-01.1"]["control_id"] = "SEC-01"
    with pytest.raises(ValueError):
        Catalog(data, schema)


def test_wrong_profile_or_app_is_not_a_smaller_successful_run(catalog):
    with pytest.raises(ValueError, match="unregistered"):
        Runner(catalog, {}).run(
            profile_id="demo-gate", profile_version="1", app="other", environment=ENV
        )
    with pytest.raises(ValueError, match="unregistered"):
        execute(catalog, profile="full-but-empty")


def test_registration_typo_rejected(catalog):
    with pytest.raises(ValueError, match="registration"):
        Runner(catalog, {"IAM-01.999": good})


def test_snapshot_of_registry(catalog):
    probes = {"IAM-01.1": good}
    runner = Runner(catalog, probes, clock=lambda: NOW)
    probes.clear()
    assert (
        runner.run(profile_id="demo-gate", profile_version="1", app="demo-chat", environment=ENV)[
            "results"
        ][0]["verdict"]
        == "PASS"
    )


def test_clock_regression_rejected(catalog):
    ticks = iter([NOW, NOW - timedelta(seconds=1)])
    with pytest.raises(ValueError, match="clock"):
        Runner(catalog, {}, clock=lambda: next(ticks)).run(
            profile_id="demo-gate", profile_version="1", app="demo-chat", environment=ENV
        )


def test_no_llm_or_command_execution_dependency():
    import ast

    import bravo_controls.runner as module

    tree = ast.parse(Path(module.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not modules & {"openai", "anthropic", "langchain", "subprocess", "boto3"}
