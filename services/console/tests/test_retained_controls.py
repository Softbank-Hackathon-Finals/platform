import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bravo_console.retained_controls import RetainedRun

NOW = datetime(2026, 11, 8, tzinfo=UTC)


@pytest.fixture
def retained(tmp_path):
    # T5 reference fixture, never actual platform execution evidence.
    raw = (Path(__file__).parents[3] / "docs/controls/examples/source-run.json").read_bytes()
    value = json.loads(raw)
    path = tmp_path / "run.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return RetainedRun(
        "fixture-tenant",
        value["environment"]["id"],
        "source",
        value["environment"]["ref"],
        value["app"],
        "demo-gate",
        "1",
        path,
        hashlib.sha256(raw).hexdigest(),
    )


def test_retained_results_are_history_even_with_recorded_passes(retained):
    result = retained.read(NOW)
    assert result["status"] == "HISTORY" and len(result["rows"]) == 3
    assert result["authorizes_execution"] is False and result["current_resource_verified"] is False
    assert any(r["recorded_verdict"] == "PASS" for r in result["rows"])
    assert all(r["evidence_state"] == "EXPIRED" for r in result["rows"])
    assert "observed" not in str(result) and "uri" not in str(result)


def test_retained_history_cannot_cross_the_registered_tenant(retained):
    from test_kubernetes_workload import registration

    from bravo_console.observed_workload import ObservedWorkloads

    r = registration()
    candidate = replace(
        retained,
        environment_id=r.environment_id,
        lane=r.lane,
        application_id="registered-chat",
        tenant_id="different-tenant",
    )
    with pytest.raises(ValueError, match="retained_observation_scope_mismatch"):
        ObservedWorkloads(
            application_id="registered-chat",
            source_revision="a" * 40,
            registrations=[r],
            collect=lambda _: None,
            retained_runs=[candidate],
        )


@pytest.mark.parametrize(
    "change", ["hash", "scope", "duplicate", "missing", "world_readable", "future", "symlink"]
)
def test_changed_or_unbound_retained_run_is_withdrawn(retained, change, tmp_path):
    if change == "hash":
        retained.path.write_bytes(b"{}")
    elif change == "scope":
        retained = replace(retained, environment_id="other")
    elif change == "world_readable":
        retained.path.chmod(0o644)
    elif change == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(retained.path)
        retained = replace(retained, path=link)
    else:
        doc = json.loads(retained.path.read_bytes())
        if change == "duplicate":
            doc["results"][1] = doc["results"][0]
        if change == "missing":
            doc["results"].pop()
        if change == "future":
            doc["finished_at"] = "2030-01-01T00:00:00Z"
        raw = json.dumps(doc).encode()
        retained.path.write_bytes(raw)
        retained = replace(retained, sha256=hashlib.sha256(raw).hexdigest())
    result = retained.read(NOW)
    assert result["status"] == "UNAVAILABLE" and not result["rows"]
    assert result["document_sha256"] is None
