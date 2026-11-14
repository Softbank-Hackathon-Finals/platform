"""ADR-0034 both.control_probes: every (kind, test) cell is a collector or explicitly UNSUPPORTED."""

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bravo_controls import Catalog, Environment, Runner, default_registry
from bravo_controls.registry import (
    PLAN_REJECTION_PREFIX,
    SLOT,
    UNSUPPORTED,
    ProbeRegistry,
    UnsupportedProbe,
)
from bravo_controls.runner import KIND_NEUTRAL_CONTROLS, KINDS

ROOT = Path(__file__).resolve().parents[3]
SERVICE = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "docs/contracts/environment-kinds.json").read_text())
PROFILES = json.loads((ROOT / "docs/controls/profiles.json").read_text())
SCHEMA = json.loads((ROOT / "docs/controls/control-result.schema.json").read_text())
FULL = next(p for p in PROFILES["profiles"] if p["id"] == "full")["required_test_ids"]
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def readme_matrix():
    text = (SERVICE / "README.md").read_text()
    body = text.split("<!-- control-probe-matrix:start -->", 1)[1]
    body = body.split("<!-- control-probe-matrix:end -->", 1)[0]
    rows = [line for line in body.strip().splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in rows[0].strip("|").split("|")]
    assert header == ["시험", *KINDS]
    table = {kind: {} for kind in KINDS}
    for line in rows[2:]:
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        for kind, cell in zip(KINDS, cells[1:], strict=True):
            table[kind][cells[0]] = cell
    return table


def test_runner_constants_follow_the_environment_kind_contract():
    assert tuple(sorted(CONTRACT["kinds"])) == KINDS
    assert set(CONTRACT["kind_neutral_controls"]) == KIND_NEUTRAL_CONTROLS
    assert CONTRACT["unsupported"] == UNSUPPORTED
    assert CONTRACT["plan_rejection_prefix"] == PLAN_REJECTION_PREFIX
    assert SLOT in CONTRACT["slots"]


def test_every_kind_and_test_cell_is_a_collector_or_explicitly_unsupported():
    registry = default_registry()
    matrix = registry.matrix(FULL)
    assert set(matrix) == set(KINDS)
    for kind in KINDS:
        assert list(matrix[kind]) == FULL
        for test_id in FULL:
            factory = registry.factory(kind, test_id)
            if matrix[kind][test_id] == UNSUPPORTED:
                assert factory is None
                probes = registry.probes(Environment("e", "target", "r", kind), [test_id], {})
                assert isinstance(probes[test_id], UnsupportedProbe)
            else:
                # The registered collector class really exists in this package.
                assert factory is not None
                cls = matrix[kind][test_id].split("+", 1)[0]
                modules = [
                    importlib.import_module(f"bravo_controls.{m.stem}")
                    for m in (SERVICE / "src/bravo_controls").glob("*.py")
                    if m.stem not in {"__init__", "app_process_tls_client"}
                ]
                assert any(callable(getattr(m, cls, None)) for m in modules), cls


def test_readme_table_matches_the_registry():
    assert readme_matrix() == default_registry().matrix(FULL)


def test_kind_neutral_controls_share_one_factory():
    registry = default_registry()
    for test_id in FULL:
        if test_id.rsplit(".", 1)[0] in KIND_NEUTRAL_CONTROLS:
            factories = {registry.factory(kind, test_id) for kind in KINDS}
            assert len(factories) == 1 and None not in factories


def test_unsupported_cell_is_unknown_and_blocks_with_marked_evidence():
    catalog = Catalog(PROFILES, SCHEMA)
    env = Environment("gcp-target", "target", "gcp:gke/fixture", "gcp")
    evidence = []
    probes = default_registry().probes(
        env, FULL, {}, clock=lambda: NOW, evidence_sink=evidence.append
    )
    run = Runner(catalog, probes, clock=lambda: NOW).run(
        profile_id="full", profile_version="1", app="demo-chat", environment=env
    )
    rows = {row["test_id"]: row for row in run["results"]}
    unsupported = rows["IAM-01.2"]
    assert unsupported["verdict"] == "UNKNOWN"
    assert unsupported["unknown_reason"] == "NOT_RUN"
    assert unsupported["evidence"]["tool"] == UNSUPPORTED
    assert json.loads(evidence[0])["reason"] == UNSUPPORTED
    # Supported but not wired yet: NOT_RUN without evidence, distinct from UNSUPPORTED.
    assert rows["SEC-01.1"]["unknown_reason"] == "NOT_RUN" and rows["SEC-01.1"]["evidence"] is None
    assert all(row["verdict"] == "UNKNOWN" for row in run["results"])
    assert "kind" not in run["environment"]  # T5 ControlRun schema is unchanged


def test_unsupported_required_positive_control_is_not_reported_as_failed_control():
    catalog = Catalog(PROFILES, SCHEMA)
    env = Environment("t", "target", "r", "onprem")
    registry = ProbeRegistry()  # nothing registered: every cell is UNSUPPORTED
    probes = registry.probes(env, FULL, {}, clock=lambda: NOW)
    run = Runner(catalog, probes, clock=lambda: NOW).run(
        profile_id="full", profile_version="1", app="demo-chat", environment=env
    )
    for row in run["results"]:
        assert (row["verdict"], row["unknown_reason"]) == ("UNKNOWN", "NOT_RUN")
        assert row["evidence"]["tool"] == UNSUPPORTED
    assert registry.missing("onprem", ["SEC-01.1"]) == (
        "adapter_missing:both.control_probes:SEC-01.1",
    )
    assert default_registry().missing("gcp", FULL) == (
        "adapter_missing:both.control_probes:IAM-01.2",
    )
    assert default_registry().missing("aws", FULL) == ()
    assert default_registry().missing("onprem", FULL) == ()


def test_environment_kind_is_optional_but_validated():
    assert Environment("s", "source", "r").kind is None
    assert Environment("s", "source", "r", "aws").kind == "aws"
    with pytest.raises(ValueError):
        Environment("s", "source", "r", "azure")
    with pytest.raises(ValueError):
        default_registry().probes(Environment("s", "source", "r"), FULL, {})


def test_registration_rules():
    registry = ProbeRegistry()
    with pytest.raises(ValueError):
        registry.register("aws", "ENC-01.1", lambda **_: None)
    with pytest.raises(ValueError):
        registry.register_neutral("SEC-01.1", lambda **_: None)
    with pytest.raises(ValueError):
        registry.register("azure", "SEC-01.1", lambda **_: None)
    registry.register("aws", "SEC-01.1", lambda **_: "probe")
    with pytest.raises(ValueError):
        registry.register("aws", "SEC-01.1", lambda **_: "probe")
    with pytest.raises(ValueError):
        registry.probes(Environment("s", "source", "r", "aws"), ["SEC-01.1"], {"SEC-01.3": {}})
    assert registry.probes(
        Environment("s", "source", "r", "aws"), ["SEC-01.1"], {"SEC-01.1": {}}
    ) == {"SEC-01.1": "probe"}


class _Search:
    pass


def test_adapter_of_another_kind_is_refused():
    registry = default_registry()
    env = Environment("s", "source", "r", "aws")
    with pytest.raises(ValueError, match="does not match environment kind"):
        registry.probes(env, ["SEC-01.3"], {"SEC-01.3": {"search": _Search()}})
    with pytest.raises(ValueError, match="does not match environment kind"):
        registry.probes(env, ["AUD-01.1"], {"AUD-01.1": {"search": _Search()}})
    from bravo_controls.aws_probes import ObjectTarget

    seaweed = ObjectTarget(
        "demo-chat", env, "", "us-east-1", "chat", "k", "v1",
        provider="s3-compatible", endpoint_url="https://seaweedfs:8333",
    )
    with pytest.raises(ValueError, match="does not match environment kind"):
        registry.probes(env, ["IAM-01.1"], {"IAM-01.1": {"target": seaweed}})


def test_unsupported_probe_refuses_another_scope():
    from bravo_controls.runner import RunContext, TestSpec

    probe = UnsupportedProbe("gcp", "IAM-01.2", clock=lambda: NOW)
    spec = TestSpec("IAM-01.2", "IAM-01", "1", "absent", "not_applicable")
    with pytest.raises(ValueError):
        probe(RunContext("r", "a", Environment("s", "source", "r", "aws")), spec)
    observation = probe(RunContext("r", "a", Environment("s", "source", "r", "gcp")), spec)
    assert observation.tool == UNSUPPORTED and observation.unknown_reason == "NOT_RUN"
